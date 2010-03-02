#!/usr/bin/env python
# coding:utf-8
"""Uniko's main module

Note: Everything is thread-unsafe.
But what is the use of threads -- in such a small gizmo?
"""

import os.path
import sys
import time
import collections
import traceback

import irclib
import BufferingBot

import util

class Message(BufferingBot.Message):

    def is_bot_specific(self):
        """Tell whether the message is bot-specific.
        If not, any other bot connected to the server may handle it.
        """
        if self.command in ['join', 'mode']:
            return True
        elif self.command in ['privmsg', 'privnotice']:
            return irclib.is_channel(self.arguments[0])
        return False

class Channel(object):
    def __init__(self, name, weight=1):
        """name -- channel name in str
        """
        if isinstance(name, str):
            raise ValueError
        self.name = name
        self.weight = weight

    def __hash__(self):
        return hash(self.name)

    def __unicode__(self):
        return self.name

    def __eq__(self, rhs):
        if isinstance(rhs, Channel):
            return self.name == rhs.name
        return self.name == rhs

    def encode(self, *_):
        return self.name.encode(*_)

class Network(object):
    """Stores information about an IRC network.
    Also works as a message buffer when the bots are running.
    """

    def __init__(self, server_list, encoding, use_ssl=False,
                 buffer_timeout=10.0):
        self.server_list = server_list
        self.encoding = encoding
        self.bots = []
        self.use_ssl = use_ssl
        self.buffer = BufferingBot.MessageBuffer(timeout=buffer_timeout)

    def encode(self, string):
        """Safely encode the string using the network's encoding."""
        return string.encode(self.encoding, 'xmlcharrefreplace')

    def decode(self, string):
        """Safely decode the byte string using the network's encoding."""
        return string.decode(self.encoding, 'ignore')

    def irc_lower(self, value):
        if isinstance(value, str):
            return irclib.irc_lower(value)
        elif isinstance(value, str):
            return self.decode(self.irc_lower(self.encode(value)))
        elif isinstance(value, Channel):
            return Channel(name = self.irc_lower(value.name),
                           weight = value.weight)
        return value

    def add_bot(self, nickname):
        bot = UnikoBufferingBot(
            self.server_list,
            self.encode(nickname),
            b'Uniko the bot',
            reconnection_interval = 60,
            use_ssl = self.use_ssl)
        bot.set_global_buffer(self.buffer)
        bot.network = self
        self.bots.append(bot)
        return bot

    def is_one_of_us(self, nickname):
        assert isinstance(nickname, bytes)
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
        if isinstance(channel, (str, Channel)):
            channel = self.encode(channel)
        # TODO: sync weight
        return [_ for _ in self.bots if channel in _.channels]

    def get_channel(self, channel):
        """Return ircbot.Channel instance."""
        if isinstance(channel, str):
            channel = self.encode(channel)
        channel = irclib.irc_lower(channel)
        for bot in self.bots:
            if channel in bot.channels:
                return bot.channels[channel]
        return None

    def get_oper(self, channel):
        if isinstance(channel, (str, Channel)):
            channel = self.encode(channel)
        for bot in self.bots:
            # TODO: sync weight
            if channel not in bot.channels:
                continue
            channel_obj = bot.channels[channel]
            if channel_obj.is_oper(bot.connection.get_nickname()):
                return bot
        return None

    def push_message(self, message):
        # XXX
        if message.command in ['privmsg']:
            target, _ = message.arguments
            if irclib.is_channel(target) and \
                not self.get_bots_by_channel(target):
                self.buffer.push(Message(command='join',
                                         arguments=(target,),
                                         timestamp=0))
        self.buffer.push(message)

class StandardPipe():
    def __init__(self, networks, channels, always=None, never=None, weight=1):
        """
        networks -- list of networks
        channels -- either string or a list of strings.
                   the length of the list must equal to the length of networks
        """
        self.networks = networks
        self.bots = []
        self.channels = {}
        for i, network in enumerate(networks):
            if isinstance(channels, (list, tuple)):
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
        for _ in always or []:
            self.actions.add(_)
        for _ in never or []:
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
                # bot.channels is updated at priority -10, hence -11
                priority = -11
            else:
                priority = 0
            bot.connection.add_global_handler(action, _handler, priority)

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
                if isinstance(channel, (tuple, list)):
                    ch = network.encode(channel[i])
                else:
                    ch = network.encode(channel)
                for i in range(n):
                    if i >= len(bot_available):
                        break
                    message = Message(command='join', arguments=(ch, ))
                    bot_available[i].push_message(message)

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
#        channel = network.decode(network.irc_lower(target))
        channel_obj = network.get_channel(target)
        source = event.source()
        if not source:
            nickname = b''
        else:
            nickname = irclib.nm_to_n(source)
            if not nickname:
                return False
        if network.is_one_of_us(nickname):
            return False
        eventtype = event.eventtype().lower()
        assert isinstance(eventtype, str)
        if eventtype not in self.actions:
            return False
        elif eventtype in ['mode']:
            modes = irclib.parse_channel_modes(b' '.join(event.arguments()))
            if all(_[0] == b'+' and _[1] in b'ov' for _ in modes):
                return False
        arg = [network.decode(_) for _ in event.arguments()]
        msg = self._format_event(eventtype).format(
            rnick=network.decode(self.repr_nickname(nickname, channel_obj)),
            nick=network.decode(nickname),
            event=eventtype,
            arg=arg,
            args=' '.join(arg)
        )
        if not msg:
            return False
        for target_network in self.networks:
            if target_network == network:
                continue
            target_channel = self.channels[target_network]
            args = (target_network.encode(target_channel),
                    target_network.encode(msg))
            message = Message(command='privmsg', arguments=args)
            target_network.push_message(message)
        return True

    def _format_event(self, eventtype):
        if eventtype in ['privmsg', 'pubmsg']:
            return '<{rnick}> {arg[0]}'
        elif eventtype in ['privnotice', 'pubnotice']:
            return '>{rnick}< {arg[0]}'
        elif eventtype in ['action']:
            return '\x02* {nick}\x02 {args}'
        elif eventtype in ['join']:
            return '! {nick} {event}'
        elif eventtype in ['topic']:
            return '! {nick} {event} "{arg[0]}"'
        elif eventtype in ['kick']:
            return '! {nick} {event} {arg[0]} ({arg[1]})'
        elif eventtype in ['mode']:
            return '! {nick} {event} {args}'
        elif eventtype in ['part', 'quit']:
            return '! {nick} {event} "{args}"'
        return '! {nick} {event} {args}'

    def handle_private_event(self, bot, event):
        network = bot.network
        nickname = irclib.nm_to_n(event.source() or b'')
        if network.is_one_of_us(nickname):
            return False
        nickname = network.decode(nickname)
        eventtype = event.eventtype().lower()
        assert isinstance(eventtype, str)
        if eventtype not in ['privmsg']:
            return False
        cmd, _, arg = event.arguments()[0].partition(b' ')
        if not cmd.startswith(b'\\'):
            return False
        cmd = cmd[1:]
        if cmd == b'who':
            return self.handle_who(bot, event, arg)
        if cmd == b'whois':
            return self.handle_whois(bot, event, arg)
        if cmd == b'topic':
            return self.handle_topic(bot, event, arg)
        if cmd == b'op':
            return self.handle_op(bot, event, arg)
        if cmd == b'aop':
            return self.handle_aop(bot, event, arg)
        return False

    def handle_who(self, bot, event, arg):
        arg = irclib.irc_lower(arg.strip())
        if not self.check_channel(bot, arg):
            return False
        network = bot.network
        channel = network.decode(arg)
        channel_obj = network.get_channel(channel)
        nickname = irclib.nm_to_n(event.source() or b'')
#        if not channel_there.is_secret():
#            return False
        if not channel_obj.has_user(nickname):
            return False
        for t_network in self.networks:
            if t_network == network:
                continue
            t_channel = self.channels[t_network]
            t_channel_obj = t_network.get_channel(t_network.encode(t_channel))
            if t_channel_obj is None:
                continue
            count = len(t_channel_obj.users())
            nicklist = t_network.decode(self.repr_nicklist(t_channel_obj))
            msg = 'Total {0} in {1}: {2}'.format(count, t_channel, nicklist)
            msg = network.encode(msg)
            message = Message(
                command='privmsg',
                arguments=(nickname, msg)
            )
            bot.push_message(message)
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

    def handle_op(self, bot, event, arg):
        # TODO
        return False

    def handle_aop(self, bot, event, arg):
        arg = irclib.irc_lower(arg.strip())
        if not self.check_channel(bot, arg):
            return False
        network = bot.network
        nickname = irclib.nm_to_n(event.source() or '')
        channel = network.decode(arg)
        # TODO: asynchronous?
        for t_network in self.networks:
            if t_network == network:
                continue
            t_channel = self.channels[t_network]
            t_channel = t_network.encode(t_channel)
            t_channel_obj = t_network.get_channel(t_channel)
            t_bot = t_network.get_oper(t_channel)
            if not t_bot:
                continue
            members = set(t_channel_obj.users())
            members = members.difference(t_channel_obj.opers())
            for _ in util.partition(members.__iter__(), 4): # XXX
                mode_string = b'+' + b'o' * len(_) + b' ' + b' '.join(_)
                message = Message(
                    command='mode',
                    arguments=(t_channel, mode_string)
                )
                t_bot.push_message(message)
            msg = b' '.join(members)
            msg = network.encode(t_network.decode(msg))
            message = Message(
                command='privmsg',
                arguments=(nickname, msg)
            )
            bot.push_message(message)
        return True

    def check_channel(self, bot, channel):
        """check if the channel should be handled by self."""
        channel = bot.network.decode(irclib.irc_lower(channel))
        return channel == self.channels[bot.network]

    def repr_nickname(self, nickname, channel_obj):
        """format nickname according to its mode given in the channel.
        Arguments:
        nickname -- nickname in bytes
        channel_obj -- ircbot.Channel instance
        """
        assert isinstance(nickname, bytes)
        if not channel_obj:
            return nickname
        # TODO: halfop and all the other modes
        elif channel_obj.is_oper(nickname):
            return b'@' + nickname
        elif channel_obj.is_voiced(nickname):
            return b'+' + nickname
        return b' ' + nickname

    def repr_nicklist(self, channel_obj):
        """format the channel's member list into following order:
        opers, voiced, others
        each of them alphabetized
        """
        # TODO: halfop and all the other modes
        def key(nickname):
            weight = \
                100 if channel_obj.is_oper(nickname) else \
                10 if channel_obj.is_voiced(nickname) else \
                1
            return weight, irclib.irc_lower(nickname)
        members = sorted(channel_obj.users(), key=key)
        return b' '.join(self.repr_nickname(_, channel_obj) for _ in members)

    def repr_event(self, event):
        result = [
            event.source(),
            event.target(),
            event.eventtype(),
            event.arguments(),
        ]
        return ' '.join(repr(_) for _ in result)

class UnikoBufferingBot(BufferingBot.BufferingBot):
    def __init__(self, network_list, nickname, realname,
                 reconnection_interval=60, use_ssl=False, buffer_timeout=10.0):
        BufferingBot.BufferingBot.__init__(self, network_list, nickname,
                                           realname, reconnection_interval,
                                           use_ssl, buffer_timeout)
        self.buffer = BufferingBot.MessageBuffer(timeout=buffer_timeout)
        self.last_tick = 0

    def __lt__(self, bot):
        return hash(self) < hash(bot)

    def set_global_buffer(self, buffer):
        self.global_buffer = buffer

    def on_tick(self):
        if not self.connection.is_connected():
            return
        self.flood_control()


    def flood_control(self):
        if super(UnikoBufferingBot, self).flood_control():
            return True
        if len(self.global_buffer):
            print('--- global buffer ---')
            self.global_buffer._dump()
            self.pop_buffer(self.global_buffer)
            return True
        return False

    def push_message(self, message):
        if message.is_bot_specific():
            self.buffer.push(message)
        else:
            self.global_buffer.push(message)

class UnikoBot():
    def __init__(self, config_file_name):
        self.networks = {}
        self.bots = collections.defaultdict(list)
        self.pipes = []
        self.config_file_name = config_file_name
        self.config_timestamp = self._get_config_time()
        data = eval(open(config_file_name).read())
        self.version = -1
        self.debug = False
        self.load()

    def _get_config_time(self):
        return os.stat(self.config_file_name).st_mtime

    def _get_config_data(self):
        return eval(open(self.config_file_name).read())

    def start(self):
        for _ in self.bots.values():
            for bot in _:
                print('{0._nickname} connecting to {0.server_list}'.format(bot))
                bot._connect()
        while True:
            for _ in self.bots.values():
                for bot in _:
                    bot.ircobj.process_once(0.2)
                    bot.on_tick()
            for pipe in self.pipes:
                pipe.on_tick()
            try:
                if self._get_config_time() > self.config_timestamp:
                    self.reload()
            except Exception:
                traceback.print_exc()

    def load(self):
        data = self._get_config_data()
        self.version = data['version']
        self.debug = data.get('debug', False)
        self.load_network(data['network'])
        self.load_bot(data['bot'])
        self.load_pipe(data['pipe'])

    def reload(self):
        data = self._get_config_data()
        if self.version >= data['version']:
            return
        print("reloading...")
        self.version = data['version']
        self.debug = data.get('debug', False)
        self.reload_network(data['network'])
        self.reload_bot(data['bot'])
        self.reload_pipe(data['pipe'])

    def reload_network(self, data):
        pass # TODO

    def load_network(self, data):
        for network_data in data:
            self.networks[network_data['name']] = Network(
                network_data['server'],
                encoding=network_data['encoding'],
                use_ssl=network_data.get('use_ssl', False),
                buffer_timeout=network_data.get('buffer_timeout', 10.0)
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
        # TODO: shed

def main():
    irclib.DEBUG = 1
    profile = None
    if len(sys.argv) > 1:
        profile = sys.argv[1]
    if not profile:
        profile = 'config'
    print("Using profile:", profile)
    root_path = os.path.dirname(os.path.abspath(__file__))
    config_file_name = os.path.join(root_path, '%s.py' % profile)
    uniko = UnikoBot(config_file_name)
    uniko.start()

if __name__ == '__main__':
    main()

# vim: ai et ts=4 sts=4 sw=4

