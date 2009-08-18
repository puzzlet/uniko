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

speaking:
Buffer가 Packet 여러 개를 가지고 있고
Bot 여러 개가 Buffer 하나를 공유할 수 있고 (thread-safe? -_-)
flood-control은 Bot이 알아서 따로따로
Server가 Buffer 하나를 관리하고 있고

listening:
Bot에 들어오는 메시지를 Pipe가 처리하는데
우리 Bot끼리 하는 말은 Pipe가 무시해야 하고
채널마다 listening Bot은 하나만 있어도 됨

각 채널마다 listening bot이 뭔지 speaking bot이 뭔지는 어디서 관리하나
쿼리는 그 해당 봇이 처리해야 하는데 flood-control도 해줘야 함
ㄴ buffer_append를 봇에서 알아서 처리해줘야
"""

class Packet():
    def __init__(self, command, arguments):
        self.command = command
        self.arguments = arguments
        self.timestamp = time.time()

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
        if self.command in ['join']:
            return True
        elif self.command in ['privmsg', 'privnotice']:
            return irclib.is_channel(self.arguments[0])
        return False

class PacketBuffer(object):
    def __init__(self, timeout=10.0):
        self.timeout = timeout
        self.heap = [] # Do we need thread safety here?

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
            packet = self._pop()
            if packet.command in ['privmsg', 'privnotice']:
                target, message = packet.arguments
                if not message.startswith('--'): # XXX
                    line_counts[target] += 1
        for target, line_count in line_counts.iteritems():
            message = "-- Message lags over %f seconds. Skipping %d line(s).." \
                % (self.timeout, line_count)
            packet = Packet(
                command = 'privmsg',
                arguments = (target, message)
            )
            self.push(packet)

class Server(object):
    """Stores information about an IRC network.
    Also works as a packet buffer when the bot is running.
    """

    def __init__(self, server_list, encoding, use_ssl=False):
        self.server_list = server_list
        self.encoding = encoding
        self.use_ssl = use_ssl
        self.bot = None # XXX
        self.buffer = PacketBuffer(timeout=10.0)

    def encode(self, string):
        """Safely encode the string using the server's encoding."""
        return string.encode(self.encoding, 'xmlcharrefreplace')

    def decode(self, string):
        """Safely decode the string using the server's encoding."""
        return string.decode(self.encoding, 'ignore')

    def get_channel(self, channel):
        """Return ircbot.Channel instance."""
        if type(channel) == unicode:
            channel = self.encode(channel)
        return self.bot.channels.get(channel, None)

    def is_one_of_us(self, nickname):
        # TODO: multiple bot
        nicknames = [bot.connection.get_nickname() for bot in [self.bot]]
        return nickname in nicknames

    def get_nickname(self):
        """Return the bot's nickname in the server."""
        # TODO: multiple bot
        return self.bot.connection.get_nickname()
 
class Handler(object):
    def handle(self, connection, event):
        raise NotImplemented

    def attach_handler(self, server):
        raise NotImplemented

class StandardPipe(Handler):
    def __init__(self, servers, channels):
        self.servers = servers
        self.channels = collections.defaultdict(dict)
        for channel in channels:
            channel_map = self._channel_map(servers, channel)
            for i, server in enumerate(servers):
                if type(channel) in [str, unicode]:
                    key = channel
                else:
                    key = channel[i]
                self.channels[server][key] = channel_map

    def _channel_map(self, servers, channel):
        """Return (server -> channel) dict
        servers -- list of servers
        channel -- either string or a list of strings.
                   the length of the list must equal to the length of servers
        """
        result = {}
        for i, server in enumerate(servers):
            if type(channel) in [str, unicode]:
                result[server] = channel
            elif channel[i]: # allow to be empty
                result[server] = channel[i]
        return result

    def attach_handler(self, server):
        def _on_connected(connection, event):
            for channel in self.channels[server].keys():
                channel = channel.encode(server.encoding)
                packet = Packet(command='join', arguments=(channel,))
                server.bot.push_packet(packet)
        server.bot.connection.add_global_handler('created', _on_connected)
        def _handler(_, event):
            self.handle(server, event)
        server.bot.connection.add_global_handler('action', _handler, 0)
        server.bot.connection.add_global_handler('join', _handler, 0)
        server.bot.connection.add_global_handler('kick', _handler, 0)
        server.bot.connection.add_global_handler('mode', _handler, 0)
        server.bot.connection.add_global_handler('part', _handler, 0)
        server.bot.connection.add_global_handler('privmsg', _handler, 0)
        server.bot.connection.add_global_handler('privnotice', _handler, 0)
        server.bot.connection.add_global_handler('pubmsg', _handler, 0)
        server.bot.connection.add_global_handler('pubnotice', _handler, 0)
        server.bot.connection.add_global_handler('topic', _handler, 0)
        # "global" relay, where no target is specified
        # it should be called before each of bot.channels is updated, hence -11
        server.bot.connection.add_global_handler('nick', _handler, -11)
        server.bot.connection.add_global_handler('quit', _handler, -11)

    def handle(self, server, event):
        if server not in self.servers:
            return
        target = event.target()
        try:
            if irclib.is_channel(target):
                handled = self.handle_channel_event(server, event)
            else:
                handled = self.handle_private_event(server, event)
        except:
            traceback.print_exc()
            handled = False
        if not handled:
            util.trace('Unhandled message: %s' % self.repr_event(event))

    def handle_channel_event(self, server, event):
        source = event.source()
        if not source:
            nickname = ''
        else:
            nickname = irclib.nm_to_n(source)
            if not nickname:
                return False
        if server.is_one_of_us(nickname):
            return False
        target = event.target()
        if not self.check_channel(server, target):
            return False
        channel = server.decode(target)
        channel_obj = server.get_channel(target)
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
            target_server.bot.push_packet(packet)
        return True

    def handle_private_event(self, server, event):
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
            return self.handle_who(server, event, arg)
        if cmd == 'whois':
            return self.handle_whois(server, event, arg)
        if cmd == 'topic':
            return self.handle_topic(server, event, arg)
        elif cmd == 'op':
            pass # TODO
        elif cmd == 'aop':
            return self.handle_aop(server, arg)
        return False

    def handle_who(self, server, event, arg):
        if not self.check_channel(server, arg):
            return False
        nickname = irclib.nm_to_n(event.source() or '')
        channel = server.decode(arg)
        channel_obj = server.bot.channels.get(arg, None)
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
            members = channel_obj.users()
            msg = 'Total %d in %s: %s' % (len(members), target_channel,
                 self.repr_nicklist(target_channel_obj))
            msg = server.encode(target_server.decode(msg))
            packet = Packet(
                command = 'privmsg',
                arguments = (nickname, msg)
            )
            server.bot.push_packet(packet)
        return True

    def handle_whois(self, server, event, arg):
        """shows whois information from the other sides.
        Usage: /msg uniko \whois nickname
               where nickname is the nickname in each of the servers other
               than the user is in.
        """
        # TODO: asynchronous
        return False

    def handle_topic(self, server, event, arg):
        # TODO: asynchronous
        return False

    def handle_aop(self, server, arg):
        if not self.check_channel(server, arg):
            return False
        channel = server.decode(arg)
        for target_server in self.servers:
            if target_server == server:
                continue
            target_channel = self.channels[server][channel][target_server]
            target_channel = target_server.encode(target_channel)
            target_channel_obj = target_server.get_channel(target_channel)
            if not target_channel_obj.is_oper(target_server.get_nickname()):
                # TODO: multiple bot
                continue
            members = set(target_channel_obj.users())
            members = members.difference(target_channel_obj.opers())
            for _ in util.partition(members.__iter__(), 4): # XXX
                mode_string = '+%s %s' % ('o' * len(_), ' '.join(_))
                packet = Packet(
                    command = 'mode',
                    arguments = mode_string
                )
                target_server.bot.push_packet(packet)
            msg = ' '.join(members)
            msg = server.encode(target_server.decode(msg))
            packet = Packet(
                command = 'privmsg',
                arguments = msg
            )
            server.bot.push_packet(packet)
        return True

    def check_channel(self, server, channel):
        """checks if this should listen to the channel in the server."""
        if not irclib.is_channel(channel):
            return False
        channel_obj = server.get_channel(channel)
        if not channel_obj:
            return False
        channel = server.decode(channel)
        if channel not in self.channels[server]:
            return False
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
                                           realname, reconnection_interval)
        self.connection.add_global_handler('created', self._on_connected)
        self.use_ssl = use_ssl
        self.buffer = PacketBuffer(10.0)

    def _on_connected(self, connection, event):
        self.ircobj.execute_delayed(0, self.flood_control)

    def set_global_buffer(self, buffer):
        self.global_buffer = buffer

    def flood_control(self):
        if len(self.buffer):
            packet = self.buffer.peek()
        elif len(self.global_buffer):
            packet = self.global_buffer.peek()
        else:
            packet = None
        if packet is not None:
            delay = 2
            if packet.command == 'privmsg':
                msg = packet.arguments[1]
                delay = 0.5 + len(msg) / 35.
            if delay > 4:
                delay = 4
            self.ircobj.execute_delayed(delay, self.pop_packet)
        self.ircobj.execute_delayed(0.1, self.flood_control)

    def pop_packet(self):
        if len(self.buffer):
            packet = self.buffer.pop()
        elif len(self.global_buffer):
            packet = self.global_buffer.pop()
        else:
            return
        if not packet:
            return
        try:
            if packet.command == 'privmsg':
                self.connection.privmsg(*packet.arguments)
            elif packet.command == 'privnotice':
                self.connection.privnotice(*packet.arguments)
            elif packet.command == 'join':
                self.connection.join(*packet.arguments)
        except irclib.ServerNotConnectedError:
            self.push_packet(packet)
            self._connect()
        except:
            traceback.print_exc()
            self.push_packet(packet)

    def push_packet(self, packet):
        print '*** push_packet ***'
        print packet
        if packet.is_bot_specific():
            self.buffer.push(packet)
        else:
            self.global_buffer.push(packet)

class UnikoBot():
    def __init__(self, nickname='uniko'):
        self.nickname = nickname
        self.servers = set()

    def add_server(self, server):
        if server not in self.servers:
            self.servers.add(server)
            server.bot = BufferingBot(
                server.server_list,
                self.nickname,
                'Uniko',
                reconnection_interval = 600,
                use_ssl = server.use_ssl
            )
            server.bot.set_global_buffer(server.buffer)

    def add_pipe(self, pipe):
        for server in pipe.servers:
            self.add_server(server)
            pipe.attach_handler(server)

    def start(self):
        for server in self.servers:
            server.bot._connect()
        while True:
            for server in self.servers:
                server.bot.ircobj.process_once(0.2)

# vim: et ts=4 sts=4 sw=4

