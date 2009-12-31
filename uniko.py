#!/usr/bin/env python
# coding:utf-8
import os.path
import sys
import time
import collections
import heapq
import signal
import imp
import traceback

import irclib
import ircbot

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
    Note that this uses heapq mechanism hence not thread-safe.
    """

    def __init__(self, timeout=10.0):
        self.timeout = timeout
        self.heap = []

    def __len__(self):
        return len(self.heap)

    def _dump(self):
        print self.heap

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

    def has_buffer_by_command(self, command):
        return any(_.command == command for _ in self.heap)

class Channel(object):
    def __init__(self, name, weight=1):
        """name -- channel name in unicode
        """
        if type(name) != unicode:
            raise ValueError
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

class Network(object):
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
        """Safely encode the string using the network's encoding."""
        return string.encode(self.encoding, 'xmlcharrefreplace')

    def decode(self, string):
        """Safely decode the string using the network's encoding."""
        return string.decode(self.encoding, 'ignore')

    def irc_lower(self, value):
        if type(value) == str:
            return irclib.irc_lower(value)
        elif type(value) == unicode:
            return self.decode(self.irc_lower(self.encode(value)))
        elif type(value) == Channel:
            return Channel(name = self.irc_lower(value.name),
                           weight = value.weight)
        return value

    def add_bot(self, nickname):
        bot = BufferingBot(
            self.server_list,
            nickname,
            'Uniko the bot',
            reconnection_interval = 60,
            use_ssl = self.use_ssl)
        bot.set_global_buffer(self.buffer)
        bot.network = self
        self.bots.append(bot)
        return bot

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
        channel = irclib.irc_lower(channel)
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
        """Return the bot's nickname in the network."""
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
        raise NotImplementedError

    def attach_handler(self, network):
        raise NotImplementedError

    def on_tick(self):
        raise NotImplementedError

class StandardPipe(Handler):
    def __init__(self, networks, channels, always=[], never=[], weight=1):
        """
        networks -- list of networks
        channels -- either string or a list of strings.
                   the length of the list must equal to the length of networks
        """
        self.networks = networks
        self.bots = []
        """self.channels[network] = channel
        """
        self.channels = {}
        for i, network in enumerate(networks):
            if type(channels) in [list, tuple]:
                if not channels[i]: # allow to be None
                    continue
                self.channels[network] = network.irc_lower(channels[i])
            else:
                self.channels[network] = network.irc_lower(channels)
        self.actions = set([
            'action', 'privmsg', 'privnotice', 'pubmsg', 'pubnotice',
            'kick', 'mode', 'topic',
            # 'nick',
            # 'join', 'part', 'quit',
            ])
        for _ in always:
            self.actions.add(_)
        for _ in never:
            self.actions.remove(_)
        self.weight = weight
        self.handler_function = {}
        self.join_tick = 0

    def attach_handler(self, bot):
        def _handler(_, event):
            self.handle(bot, event)
        self.handler_function[bot] = _handler
        self.bots.append(bot)
        for action in self.actions:
            if action in ['nick', 'quit']:
                # they should be called before each of bot.channels is updated, hence -11
                priority = -11
            else:
                priority = 0
            bot.connection.add_global_handler(action, _handler, 0)

    def detach_all_handler(self):
        for bot in self.bots:
            for action in self.actions:
                bot.connection.remove_global_handler(action,
                    self.handler_function[bot])

    def on_tick(self):
        tick = time.time()
        if self.join_tick + 2 > tick:
            return
        self.join_tick = time.time()
        for channel in self.channels.values():
            for i, network in enumerate(self.networks):
                bot_joined = network.get_bots_by_channel(channel)
                n = self.weight - len(bot_joined)
                if n <= 0:
                    continue
                bot_available = []
                for bot in network.bots:
                    if bot in bot_joined:
                        continue
                    elif bot.buffer.has_buffer_by_command('join'):
                        n -= 1
                    elif len(bot.channels) >= 20: # XXX
                        continue
                    else:
                        bot_available.append(bot)
                if type(channel) in [tuple, list]:
                    ch = network.encode(channel[i])
                else:
                    ch = network.encode(channel)
                for i in range(n):
                    if i >= len(bot_available):
                        break
                    packet = Packet(command='join', arguments=(ch, ))
                    bot_available[i].push_packet(packet)

    def sync(self):
        raise NotImplementedError
        # TODO: implement
        for network in self.channels.keys():
            for channel in self.channels[network].keys():
                if type(channel) == Channel:
                    weight = channel.weight
                else:
                    weight = 1
                bots = network.get_bots_by_channel(channel)
                if len(bots) < weight:
                    for _ in xrange(weight - len(bots)):
                        pass
                elif len(bots) > weight:
                    #for bot in bots:
                    for _ in xrange(len(bots) - weights):
                        pass

    def handle(self, bot, event):
        network = bot.network
        if network not in self.networks:
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
            pass
            # util.trace('Unhandled message: %s' % self.repr_event(event))

    def handle_channel_event(self, bot, event):
        network = bot.network
        target = event.target()
        if not bot.network.is_listening_bot(bot, target):
            return False # not the channel's listening bot
        if not self.check_channel(bot, target):
            return False
        channel = network.decode(network.irc_lower(target))
        channel_obj = network.get_channel(target)
        source = event.source()
        if not source:
            nickname = ''
        else:
            nickname = irclib.nm_to_n(source)
            if not nickname:
                return False
        if network.is_one_of_us(nickname):
            return False
        arg = event.arguments()
        msg = None
        eventtype = event.eventtype().lower()
        if eventtype not in self.actions:
            return False
        elif eventtype in ['privmsg', 'pubmsg']:
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
        elif eventtype in ['part', 'quit']:
            msg = '! %s %s %s' % (nickname, eventtype, ' '.join(arg))
        elif eventtype in ['action']:
            msg = '\x02* %s\x02 %s' % (nickname, ' '.join(arg))
        else:
            msg = '! %s %s %s' % (nickname, eventtype, repr(arg))
            util.trace('Unexpected message: %s' % repr(msg))
        if not msg:
            return False
        msg = network.decode(msg)
        for target_network in self.networks:
            if target_network == network:
                continue
            target_channel = self.channels[target_network]
            args = (target_network.encode(target_channel),
                    target_network.encode(msg))
            packet = Packet(command='privmsg', arguments=args)
            target_network.push_packet(packet)
        return True

    def handle_private_event(self, bot, event):
        network = bot.network
        nickname = irclib.nm_to_n(event.source() or '')
        if network.is_one_of_us(nickname):
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
        network = bot.network
        channel = network.decode(arg)
        channel_obj = network.get_channel(channel)
        nickname = irclib.nm_to_n(event.source() or '')
        if not channel_obj.has_user(nickname):
            # or (not channel_there.is_secret())
            return False
            # TODO: what will happen if nickname isn't ascii?
        for target_network in self.networks:
            if target_network == network:
                continue
            target_channel = self.channels[target_network]
            target_channel = target_network.encode(target_channel)
            target_channel_obj = target_network.get_channel(target_channel)
            if target_channel_obj is None:
                continue
            members = target_channel_obj.users()
            msg = 'Total %d in %s: %s' % (len(members), target_channel,
                 self.repr_nicklist(target_channel_obj))
            msg = network.encode(target_network.decode(msg))
            packet = Packet(
                command='privmsg',
                arguments=(nickname, msg)
            )
            bot.push_packet(packet)
        return True

    def handle_whois(self, bot, event, arg):
        """shows whois information from the other sides.
        Usage: /msg uniko \whois nickname
               where nickname is the nickname in each of the networks other
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
        network = bot.network
        nickname = irclib.nm_to_n(event.source() or '')
        channel = network.decode(arg)
        # TODO: asynchronous?
        for target_network in self.networks:
            if target_network == network:
                continue
            target_channel = self.channels[target_network]
            target_channel = target_network.encode(target_channel)
            target_channel_obj = target_network.get_channel(target_channel)
            target_bot = target_network.get_oper(target_channel)
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
            msg = network.encode(target_network.decode(msg))
            packet = Packet(
                command='privmsg',
                arguments=(nickname, msg)
            )
            bot.push_packet(packet)
        return True

    def check_channel(self, bot, channel):
        """check if the channel should be handled by self."""
        channel = bot.network.decode(irclib.irc_lower(channel))
        return channel == self.channels[bot.network]

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
    def __init__(self, network_list, nickname, realname,
                 reconnection_interval=60, use_ssl=False):
        ircbot.SingleServerIRCBot.__init__(self, network_list, nickname,
                                           realname, reconnection_interval,
                                           use_ssl)
        self.buffer = PacketBuffer(10.0)
        self.last_tick = 0

    def set_global_buffer(self, buffer):
        self.global_buffer = buffer

    def on_tick(self):
        if not self.connection.is_connected():
            return
        self.flood_control()

    def get_delay(self, packet):
        # TODO: per-network configuration
        delay = 0
        if packet.command == 'privmsg':
            delay = 2
            try:
                target, msg = packet.arguments
                delay = 0.5 + len(msg) / 35.
            except:
                traceback.print_exc()
        if delay > 4:
            delay = 4
        return delay

    def flood_control(self):
        """Delays message according to the length of packet.
        As you see, this doesn't acquire any lock hence thread-unsafe.
        """
        if not self.connection.is_connected():
            self._connect()
            return
        packet = None
        local = False
        if len(self.buffer):
            print '--- buffer ---'
            self.buffer._dump()
            self.pop_buffer(self.buffer)
        elif len(self.global_buffer):
            print '--- global buffer ---'
            self.global_buffer._dump()
            self.pop_buffer(self.global_buffer)

    def pop_buffer(self, buffer):
        if not buffer:
            return
        packet = buffer.peek()
        if packet.command == 'privmsg':
            try:
                target, msg = packet.arguments
                if irclib.is_channel(target) and target not in self.channels:
                    return
            except:
                traceback.print_exc()
                return
        delay = self.get_delay(packet)
        tick = time.time()
        if self.last_tick + delay > tick:
            return
        self.process_packet(packet)
        assert packet == buffer.pop()
        self.last_tick = tick

    def process_packet(self, packet):
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
    def __init__(self, config_file_name):
        self.networks = {}
        self.bots = collections.defaultdict(list)
        self.pipes = []
        self.config_file_name = config_file_name
        self.config_timestamp = os.stat(self.config_file_name).st_mtime
        data = eval(open(config_file_name).read())
        self.load(data)

    def start(self):
        for _ in self.bots.itervalues():
            for bot in _:
                print '%s Connecting to %s' % (bot._nickname,
                    repr(bot.server_list))
                bot._connect()
        while True:
            for _ in self.bots.itervalues():
                for bot in _:
                    bot.ircobj.process_once(0.2)
                    bot.on_tick()
            for pipe in self.pipes:
                pipe.on_tick()
            try:
                t = os.stat(self.config_file_name).st_mtime
                if t <= self.config_timestamp:
                    continue
                data = eval(open(self.config_file_name).read())
                if self.version >= data['version']:
                    continue
                print "reloading"
                self.reload(data)
            except:
                traceback.print_exc()

    def load(self, data):
        self.load_network(data['network'])
        self.load_bot(data['bot'])
        self.load_pipe(data['pipe'])
        self.version = data['version']

    def reload(self, data):
        self.reload_network(data['network'])
        self.reload_bot(data['bot'])
        self.reload_pipe(data['pipe'])
        self.version = data['version']

    def reload_network(self, data):
        pass # TODO

    def load_network(self, data):
        for network_data in data:
            self.networks[network_data['name']] = Network(
                network_data['server'],
                encoding=network_data['encoding'],
                use_ssl=network_data.get('use_ssl', False)
            )

    def reload_bot(self, data):
        pass # TODO

    def load_bot(self, data):
        for bot_data in data:
            network = self.networks[bot_data['network']]
            bot = network.add_bot(nickname=bot_data['nickname'])
            self.bots[bot_data['network']].append(bot)

    def reload_pipe(self, data):
        while self.pipes:
            pipe = self.pipes.pop()
            pipe.detach_all_handler()
        self.load_pipe(data)

    def load_pipe(self, data):
        print data
        for pipe_data in data:
            networks = [self.networks[_] for _ in pipe_data['network']]
            pipe = StandardPipe(networks=networks,
                channels=pipe_data['channel'],
                always=pipe_data.get('always', []),
                never=pipe_data.get('never', []),
                weight=pipe_data.get('weight', 1)
            )
            for network in pipe_data['network']:
                for bot in self.bots[network]:
                    pipe.attach_handler(bot)
            self.pipes.append(pipe)

def main():
    irclib.DEBUG = 1
    profile = None
    if len(sys.argv) > 1:
        profile = sys.argv[1]
    if not profile:
        profile = 'config'
    print profile
    UNIKO_ROOT = os.path.dirname(os.path.abspath(__file__))
    config_file_name = os.path.join(UNIKO_ROOT, '%s.py' % profile)
    uniko = UnikoBot(config_file_name)
    uniko.start()

if __name__ == '__main__':
    main()

# vim: ai et ts=4 sts=4 sw=4

