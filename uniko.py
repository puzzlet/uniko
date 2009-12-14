#!/usr/bin/env python
# coding:utf-8
import time
import collections
import heapq
import irclib
import ircbot
import traceback

import util

"""Uniko's main module

Note - Everything is thread-unsafe.
       Why use thread anyway -- this project is a mere contraption!
"""

class Packet():
    def __init__(self, command, arguments, timestamp=None):
        self.command = command
        self.arguments = arguments
        self.timestamp = time.time() if timestamp is None else timestamp

    def __repr__(self):
        return '<Packet %s %s %s>' % (
            repr(self.command),
            repr(self.arguments),
            repr(self.timestamp)
        )

    def __cmp__(self, packet):
        return cmp(self.timestamp, packet.timestamp)

    def is_bot_specific(self):
        """Tell whether the packet is bot-specific.
        If not, any other bot connected to the server may handle it.
        """
        if self.command in ['join', 'mode']:
            return True
        elif self.command in ['privmsg', 'privnotice']:
            return irclib.is_channel(self.arguments[0])
        return False

    def is_system_message(self):
        if self.command in ['privmsg', 'privnotice']:
            return self.arguments[1].startswith('--') # XXX
        return False

class PacketBuffer(object):
    """Buffer of Packet objects, sorted by their timestamp.
    If some of its Packet's timestamp lags over self.timeout, it purges all the queue.
    Note that this uses heapq mechanism hence thread-unsafe.
    """

    def __init__(self, timeout=10.0):
        self.timeout = timeout
        self.heap = []

    def __len__(self):
        return len(self.heap)

    def peek(self):
        return self.heap[0]

    def push(self, packet):
        return heapq.heappush(self.heap, packet)

    def _pop(self):
        if not self.heap:
            return None
        return heapq.heappop(self.heap)

    def pop(self):
        if self.peek().timestamp < time.time() - self.timeout:
            self.purge()
        return self._pop()

    def purge(self):
        stale = time.time() - self.timeout
        line_counts = collections.defaultdict(int)
        while self.heap:
            packet = self.peek()
            if packet.timestamp > stale:
                break
            if packet.command in ['join']: # XXX
                break
            packet = self._pop()
            if packet.command in ['privmsg', 'privnotice']:
                try:
                    target, message = packet.arguments
                except:
                    traceback.print_exc()
                    self.push(packet)
                    return
                if not packet.is_system_message():
                    line_counts[target] += 1
        for target, line_count in line_counts.iteritems():
            message = "-- Message lags over %f seconds. Skipping %d line(s).." \
                % (self.timeout, line_count)
            packet = Packet(
                command = 'privmsg',
                arguments = (target, message)
            )
            self.push(packet)

class Channel(object):
    def __init__(self, name, weight=1):
        """name -- channel name in unicode
        """
        self.name = name
        self.weight = weight

    def __hash__(self):
        return hash(self.name)

    def __unicode__(self):
        return self.name

    def __eq__(self, rhs):
        if type(rhs) == Channel:
            return self.name == rhs.name
        return self.name == rhs

    def encode(self, *_):
        return self.name.encode(*_)

class Server(object):
    """Stores information about an IRC network.
    Also works as a packet buffer when the bots are running.
    """

    def __init__(self, server_list, encoding, use_ssl=False):
        self.server_list = server_list
        self.encoding = encoding
        self.use_ssl = use_ssl
        self.bots = []
        self.buffer = PacketBuffer(timeout=10.0)

    def encode(self, string):
        """Safely encode the string using the server's encoding."""
        return string.encode(self.encoding, 'xmlcharrefreplace')

    def decode(self, string):
        """Safely decode the string using the server's encoding."""
        return string.decode(self.encoding, 'ignore')

    def add_bot(self, nickname):
        bot = BufferingBot(
            self.server_list,
            nickname,
            'Uniko',
            reconnection_interval = 60,
            use_ssl = self.use_ssl)
        bot.set_global_buffer(self.buffer)
        bot.server = self
        self.bots.append(bot)

    def is_one_of_us(self, nickname):
        nicknames = [bot.connection.get_nickname() for bot in self.bots]
        return nickname in nicknames

    def is_listening_bot(self, bot, channel):
        """Tell whether the bot is on of the "listening bots" for the channel.
        """
        if not irclib.is_channel(channel):
            return False # not even a channel
        if bot not in self.bots:
            return False
        bots = self.get_bots_by_channel(channel)
        if bot not in bots:
            return False
        bots.sort()
        return bots[0] == bot

    def get_bots_by_channel(self, channel):
        if type(channel) in [unicode, Channel]:
            channel = self.encode(channel)
        # TODO: sync weight
        return [_ for _ in self.bots if channel in _.channels]

    def get_channel(self, channel):
        """Return ircbot.Channel instance."""
        if type(channel) == unicode:
            channel = self.encode(channel)
        for bot in self.bots:
            if channel in bot.channels:
                return bot.channels[channel]
        return None

    def get_oper(self, channel):
        if type(channel) in [unicode, Channel]:
            channel = self.encode(channel)
        for bot in self.bots:
            # TODO: sync weight
            if channel not in bot.channels:
                continue
            channel_obj = bot.channels[channel]
            if channel_obj.is_oper(bot.connection.get_nickname()):
                return bot
        return None

    def get_nickname(self):
        """Return the bot's nickname in the server."""
        return self.bot.connection.get_nickname()

    def push_packet(self, packet):
        # XXX
        if packet.command == 'privmsg':
            target, msg = packet.arguments
            if irclib.is_channel(target) and not self.get_bots_by_channel(target):
                self.buffer.push(Packet(command='join',
                                        arguments=(target,),
                                        timestamp=0))
        self.buffer.push(packet)

class Handler(object):
    def handle(self, connection, event):
        raise NotImplemented

    def attach_handler(self, server):
        raise NotImplemented

class StandardPipe(Handler):
    def __init__(self, servers, channels):
        self.servers = servers
        """self.channels[server][channel][server] = channel
        """ # XXX
        self.channels = collections.defaultdict(dict)
        for channel in channels:
#            if type(channel) is Channel:
#                channel.name = irclib.irc_lower(channel.name)
#            else:
#                channel = irclib.irc_lower(channel)
            channel_map = self._channel_map(servers, channel)
            for i, server in enumerate(servers):
                if type(channel) in [list, tuple]:
                    key = channel[i]
                else:
                    key = channel
                if type(channel) in [str, unicode]:
                    key = Channel(key)
                self.channels[server][key] = channel_map

    def _channel_map(self, servers, channel):
        """Return (server -> channel) dict
        servers -- list of servers
        channel -- either string or a list of strings.
                   the length of the list must equal to the length of servers
        """
        result = {}
        for i, server in enumerate(servers):
            if type(channel) in [list, tuple]:
                if channel[i]: # allow to be None
                    result[server] = channel[i]
            else:
                result[server] = channel
        return result

    def attach_handler(self, bot):
        def _on_connected(connection, event):
            server = bot.server
            for channel in self.channels[server].keys():
                if type(channel) == Channel:
                    weight = channel.weight
                else:
                    weight = 1
                if len(server.get_bots_by_channel(channel)) >= weight:
                    continue
                channel = server.encode(channel)
                packet = Packet(command='join', arguments=(channel,))
                bot.push_packet(packet)
        bot.connection.add_global_handler('created', _on_connected)
        def _handler(_, event):
            self.handle(bot, event)
        bot.connection.add_global_handler('action', _handler, 0)
        bot.connection.add_global_handler('join', _handler, 0)
        bot.connection.add_global_handler('kick', _handler, 0)
        bot.connection.add_global_handler('mode', _handler, 0)
        bot.connection.add_global_handler('part', _handler, 0)
        bot.connection.add_global_handler('privmsg', _handler, 0)
        bot.connection.add_global_handler('privnotice', _handler, 0)
        bot.connection.add_global_handler('pubmsg', _handler, 0)
        bot.connection.add_global_handler('pubnotice', _handler, 0)
        bot.connection.add_global_handler('topic', _handler, 0)
        # "global" relay, where no target is specified
        # it should be called before each of bot.channels is updated, hence -11
        bot.connection.add_global_handler('nick', _handler, -11)
        bot.connection.add_global_handler('quit', _handler, -11)

    def sync(self):
        # TODO: implement
        for server in self.channels.keys():
            for channel in self.channels[server].keys():
                if type(channel) == Channel:
                    weight = channel.weight
                else:
                    weight = 1
                bots = server.get_bots_by_channel(channel)
                if len(bots) < weight:
                    for _ in xrange(weight - len(bots)):
                        pass
                elif len(bots) > weight:
                    #for bot in bots:
                    for _ in xrange(len(bots) - weights):
                        pass

    def handle(self, bot, event):
        server = bot.server
        if server not in self.servers:
            return
        target = event.target()
        try:
            if irclib.is_channel(target):
                handled = self.handle_channel_event(bot, event)
            else:
                handled = self.handle_private_event(bot, event)
        except:
            traceback.print_exc()
            handled = False
        if not handled:
            util.trace('Unhandled message: %s' % self.repr_event(event))

    def handle_channel_event(self, bot, event):
        server = bot.server
        target = event.target()
        if not bot.server.is_listening_bot(bot, target):
            return False # not the channel's listening bot
        if not self.check_channel(bot, target):
            return False
        channel = server.decode(target)
        channel_obj = server.get_channel(target)
        source = event.source()
        if not source:
            nickname = ''
        else:
            nickname = irclib.nm_to_n(source)
            if not nickname:
                return False
        if server.is_one_of_us(nickname):
            return False
        arg = event.arguments()
        msg = None
        eventtype = event.eventtype().lower()
        if eventtype in ['privmsg', 'pubmsg']:
            nickname = self.repr_nickname(nickname, channel_obj)
            msg = '<%s> %s' % (nickname, arg[0])
        elif eventtype in ['privnotice', 'pubnotice']:
            nickname = self.repr_nickname(nickname, channel_obj)
            msg = '>%s< %s' % (nickname , arg[0])
        elif eventtype in ['join']:
            msg = '! %s %s' % (nickname, eventtype)
        elif eventtype in ['topic'] and len(arg) == 1:
            msg = '! %s %s "%s"' % (nickname, eventtype, arg[0])
        elif eventtype in ['kick']:
            msg = '! %s %s %s (%s)' % (nickname, eventtype, arg[0], arg[1])
        elif eventtype in ['mode']:
            modes = irclib.parse_channel_modes(' '.join(arg))
            if any(_[0] != '+' or _[1] not in ['o', 'v'] for _ in modes):
                msg = '! %s %s %s' % (nickname, eventtype, ' '.join(arg))
        elif eventtype in ['part']:
            msg = '! %s %s %s' % (nickname, eventtype, ' '.join(arg))
        elif eventtype in ['action']:
            msg = '\x02* %s\x02 %s' % (nickname, ' '.join(arg))
        else:
            msg = '! %s %s %s' % (nickname, eventtype, repr(arg))
            util.trace('Unexpected message: %s' % repr(msg))
        if not msg:
            return False
        msg = server.decode(msg)
        for target_server in self.servers:
            if target_server == server:
                continue
            if eventtype in ['join', 'part']:
                continue
            target_channel = self.channels[server][channel][target_server]
            args = (target_server.encode(target_channel),
                    target_server.encode(msg))
            packet = Packet(command='privmsg', arguments=args)
            target_server.push_packet(packet)
        return True

    def handle_private_event(self, bot, event):
        server = bot.server
        nickname = irclib.nm_to_n(event.source() or '')
        if server.is_one_of_us(nickname):
            return False
        eventtype = event.eventtype().lower()
        if eventtype not in ['privmsg']:
            return False
        cmd, _, arg = event.arguments()[0].partition(' ')
        if not cmd.startswith('\\'):
            return False
        cmd = cmd[1:]
        if cmd == 'who':
            return self.handle_who(bot, event, arg)
        if cmd == 'whois':
            return self.handle_whois(bot, event, arg)
        if cmd == 'topic':
            return self.handle_topic(bot, event, arg)
        elif cmd == 'op':
            pass # TODO
        elif cmd == 'aop':
            return self.handle_aop(bot, event, arg)
        return False

    def handle_who(self, bot, event, arg):
        arg = irclib.irc_lower(arg.strip())
        if not self.check_channel(bot, arg):
            return False
        server = bot.server
        channel = server.decode(arg)
        channel_obj = server.get_channel(channel)
        nickname = irclib.nm_to_n(event.source() or '')
        if not channel_obj.has_user(nickname):
            # or (not channel_there.is_secret())
            return False
            # TODO: what will happen if nickname isn't ascii?
        for target_server in self.servers:
            if target_server == server:
                continue
            target_channel = self.channels[server][channel][target_server]
            target_channel = target_server.encode(target_channel)
            target_channel_obj = target_server.get_channel(target_channel)
            if target_channel_obj is None:
                continue
            members = target_channel_obj.users()
            msg = 'Total %d in %s: %s' % (len(members), target_channel,
                 self.repr_nicklist(target_channel_obj))
            msg = server.encode(target_server.decode(msg))
            packet = Packet(
                command='privmsg',
                arguments=(nickname, msg)
            )
            bot.push_packet(packet)
        return True

    def handle_whois(self, bot, event, arg):
        """shows whois information from the other sides.
        Usage: /msg uniko \whois nickname
               where nickname is the nickname in each of the servers other
               than the user is in.
        """
        arg = arg.strip()
        # TODO: asynchronous
        return False

    def handle_topic(self, bot, event, arg):
        # TODO: asynchronous
        return False

    def handle_aop(self, bot, event, arg):
        arg = irclib.irc_lower(arg.strip())
        if not self.check_channel(bot, arg):
            return False
        server = bot.server
        nickname = irclib.nm_to_n(event.source() or '')
        channel = server.decode(arg)
        # TODO: asynchronous?
        for target_server in self.servers:
            if target_server == server:
                continue
            target_channel = self.channels[server][channel][target_server]
            target_channel = target_server.encode(target_channel)
            target_channel_obj = target_server.get_channel(target_channel)
            target_bot = target_server.get_oper(target_channel)
            if not target_bot:
                continue
            members = set(target_channel_obj.users())
            members = members.difference(target_channel_obj.opers())
            for _ in util.partition(members.__iter__(), 4): # XXX
                mode_string = '+%s %s' % ('o' * len(_), ' '.join(_))
                packet = Packet(
                    command='mode',
                    arguments=(target_channel, mode_string)
                )
                target_bot.push_packet(packet)
            msg = ' '.join(members)
            msg = server.encode(target_server.decode(msg))
            packet = Packet(
                command='privmsg',
                arguments=(nickname, msg)
            )
            bot.push_packet(packet)
        return True

    def check_channel(self, bot, channel):
        """check if the channel should be handled by self."""
        channel = bot.server.decode(channel)
        if channel not in self.channels[bot.server]:
            return False # not in the pipe's channel list
        return True

    def repr_nickname(self, nickname, channel_obj):
        """format nickname according to its mode given in the channel.
        Arguments:
        nickname -- nickname in string
        channel_obj -- ircbot.Channel instance
        """
        if not channel_obj:
            return nickname
        # TODO: halfop and all the other modes
        elif channel_obj.is_oper(nickname):
            return '@' + nickname
        elif channel_obj.is_voiced(nickname):
            return '+' + nickname
        return ' ' + nickname

    def repr_nicklist(self, channel_obj):
        """format the channel's member list into following order:
        opers, voiced, others
        each of them alphabetized
        """
        # TODO: halfop and all the other modes
        weight = lambda _: \
            100 if channel_obj.is_oper(_) else \
            10 if channel_obj.is_voiced(_) else \
            1
        compare = lambda nick1, nick2: \
            -cmp(weight(nick1), weight(nick2)) or \
            cmp(irclib.irc_lower(nick1), irclib.irc_lower(nick2))
        members = sorted(channel_obj.users(), cmp=compare)
        return ' '.join(self.repr_nickname(_, channel_obj) for _ in members)

    def repr_event(self, event):
        result = [
            event.source(),
            event.target(),
            event.eventtype(),
            event.arguments(),
        ]
        return ' '.join(repr(_) for _ in result)

class BufferingBot(ircbot.SingleServerIRCBot):
    def __init__(self, server_list, nickname, realname,
                 reconnection_interval=60, use_ssl=False):
        ircbot.SingleServerIRCBot.__init__(self, server_list, nickname,
                                           realname, reconnection_interval,
                                           use_ssl)
        self.buffer = PacketBuffer(10.0)
        self.ircobj.execute_delayed(0, self.on_tick)

    def set_global_buffer(self, buffer):
        self.global_buffer = buffer

    @util.periodic(0.1)
    def on_tick(self):
        if not self.connection.is_connected():
            return
        self.flood_control()

    def flood_control(self):
        """Delays message according to the length of packet.
        Obviously this doesn't utilize any lock hence thread-unsafe.
        """
        packet = None
        if len(self.buffer):
            packet = self.buffer.peek()
        elif len(self.global_buffer):
            packet = self.global_buffer.peek()
        if packet is None:
            return
        delay = 2
        if packet.command == 'privmsg':
            try:
                target, msg = packet.arguments
            except:
                traceback.print_exc()
                return
            if target not in self.channels:
                return
            delay = 0.5 + len(msg) / 35.
        if delay > 4:
            delay = 4
        self.ircobj.execute_delayed(delay, self.pop_packet) # XXX

    def pop_packet(self):
        if not self.connection.is_connected():
            return
        if len(self.buffer):
            packet = self.buffer.pop()
        elif len(self.global_buffer):
            packet = self.global_buffer.pop()
        else:
            return
        if not packet:
            return
        try:
            if False:
                pass
            elif packet.command == 'join':
                self.connection.join(*packet.arguments)
            elif packet.command == 'mode':
                self.connection.mode(*packet.arguments)
            elif packet.command == 'privmsg':
                self.connection.privmsg(*packet.arguments)
            elif packet.command == 'privnotice':
                self.connection.privnotice(*packet.arguments)
            elif packet.command == 'topic':
                self.connection.topic(*packet.arguments)
            elif packet.command == 'who':
                self.connection.who(*packet.arguments)
            elif packet.command == 'whois':
                self.connection.whois(*packet.arguments)
        except irclib.ServerNotConnectedError:
            self.push_packet(packet)
            self._connect()
        except:
            traceback.print_exc()
            self.push_packet(packet)

    def push_packet(self, packet):
        if packet.is_bot_specific():
            self.buffer.push(packet)
        else:
            self.global_buffer.push(packet)

class UnikoBot():
    def __init__(self):
        self.servers = set()

    def add_server(self, server, nicknames):
        self.servers.add(server)
        for nickname in nicknames:
            server.add_bot(nickname=nickname)

    def add_pipe(self, pipe):
        for server in pipe.servers:
            self.add_server(server, [])
            for bot in server.bots:
                pipe.attach_handler(bot)

    def start(self):
        for server in self.servers:
            for bot in server.bots:
                print 'Connecting to', server.server_list
                bot._connect()
        while True:
            for server in self.servers:
                for bot in server.bots:
                    bot.ircobj.process_once(0.2)

# vim: ai et ts=4 sts=4 sw=4

