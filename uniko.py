#!/usr/bin/env python
# coding:utf-8
"""Uniko's main module

Note: Everything is thread-unsafe.
But what is the use of threads -- in such a small gizmo?
"""

import os.path
import sys
import time
import itertools
import collections
import traceback

import irclib
from BufferingBot import Message, MessageBuffer, BufferingBot

import util

class Network(object):
    """Stores information about an IRC network.
    Also works as a message buffer when the bots are running.
    """

    def __init__(self, server_list, name, encoding, use_ssl=False):
        self.server_list = server_list
        self.name = name
        self.encoding = encoding
        self.bots = []
        self.use_ssl = use_ssl

    def encode(self, string):
        """Safely encode the string using the network's encoding."""
        result = string.encode(self.encoding, 'xmlcharrefreplace')
        return result, len(result)

    def decode(self, string):
        """Safely decode the byte string using the network's encoding."""
        result = string.decode(self.encoding, 'ignore')
        return result, len(result)

    def add_bot(self, nickname, test_mode=False):
        bot = UnikoBufferingBot(
            self,
            nickname=self.encode(nickname)[0],
            realname=b'Uniko the bot',
#            reconnection_interval=60,
            use_ssl=self.use_ssl,
            test_mode=test_mode)
        self.bots.append(bot)
        return bot

    def is_one_of_us(self, nickname):
        """Tell whether the nickname belongs to one of self.bots."""
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
        if isinstance(channel, str):
            channel = self.encode(channel)[0]
        return [_ for _ in self.bots if channel in _.channels]

    def get_channel(self, channel):
        """Return ircbot.Channel instance."""
        if isinstance(channel, str):
            channel = self.encode(channel)[0]
        channel = irclib.irc_lower(channel)
        for bot in self.bots:
            if channel in bot.channels:
                return bot.channels[channel]
        return None

    def get_oper(self, channel):
        if isinstance(channel, str):
            channel = self.encode(channel)[0]
        for bot in self.bots:
            if channel not in bot.channels:
                continue
            channel_obj = bot.channels[channel]
            if channel_obj.is_oper(bot.connection.get_nickname()):
                return bot
        return None

class StandardPipe():
    def __init__(self, networks, channels, always=None, never=None, weight=1,
                 buffer_timeout=10.0):
        """
        networks -- list of networks
        channels -- either string or a list of strings.
                    the length of the list must equal to the length of networks
        """
        self.networks = networks
        self.bots = []
        self.channels = {}
        self.buffers = {}
        for i, network in enumerate(networks):
            if isinstance(channels, (list, tuple)):
                if not channels[i]: # allow to be None
                    continue
                self.channels[network] = irclib.irc_lower(channels[i])
            else:
                self.channels[network] = irclib.irc_lower(channels)
            self.buffers[network] = MessageBuffer(timeout=-1)# XXX buffer_timeout)
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

    def attach_handler(self, bot, network):
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
        bot.add_buffer(self.buffers[network])

    def detach_all_handler(self):
        for bot in self.bots:
            for action in self.actions:
                bot.connection.remove_global_handler(action,
                    self.handler_function[bot])
            for network in self.networks:
                bot.remove_buffer(self.buffers[network])

    def on_tick(self):
        tick = time.time()
        self._sync_weight(tick)

    def _sync_weight(self, tick):
        """should only be called by self.on_tick()"""
        if self.join_tick + 2 > tick:
            return
        self.join_tick = time.time()
        for network, channel in self.channels.items():
            bot_joined = network.get_bots_by_channel(channel)
            weight = self.weight - len(bot_joined)
            if weight <= 0:
                continue
            bot_available = []
            for bot in network.bots:
                if bot in bot_joined:
                    continue
                elif not bot.connection.is_connected():
                    continue
                elif bot.buffer.has_buffer_by_command('join'):
                    weight -= 1 # XXX temporary response
                elif len(bot.channels) >= 20: # XXX network's channel limit
                    continue
                else:
                    bot_available.append(bot)
            for _, bot in zip(range(weight), bot_available):
                bot.push_message(Message(command='join',
                    arguments=(channel, )))

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
        except Exception:
            traceback.print_exc()
            handled = False

    def handle_channel_event(self, bot, event):
        network = bot.network
        target = event.target()
        if not bot.network.is_listening_bot(bot, target):
            return False # not the channel's listening bot
        if not self.check_channel(bot, target):
            return False
        nickname = irclib.nm_to_n(event.source() or '')
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
        msg = self.format_event(bot, event)
        if not msg:
            return False
        for target_network in self.networks:
            if target_network == network:
                continue
            self.push_message(target_network,
                Message(command='privmsg',
                    arguments=(self.channels[target_network], msg)))
        return True

    def format_event(self, bot, event):
        network = bot.network
        eventtype = event.eventtype().lower()
        assert isinstance(eventtype, str)
        nickname = irclib.nm_to_n(event.source() or '')
        arg = [network.decode(_)[0] for _ in event.arguments()]
        if eventtype in ['privmsg', 'pubmsg']:
            format_str = '<{rnick}> {arg[0]}'
        elif eventtype in ['privnotice', 'pubnotice']:
            format_str = '>{rnick}< {arg[0]}'
        elif eventtype in ['action']:
            format_str = '\x02* {nick}\x02 {args}'
        elif eventtype in ['join']:
            format_str = '! {nick} {event}'
        elif eventtype in ['topic']:
            format_str = '! {nick} {event} "{arg[0]}"'
        elif eventtype in ['kick']:
            format_str = '! {nick} {event} {arg[0]} ({arg[1]})'
        elif eventtype in ['mode']:
            format_str = '! {nick} {event} {args}'
        elif eventtype in ['part', 'quit']:
            format_str = '! {nick} {event} "{args}"'
        else:
            format_str = '! {nick} {event} {args}'
        return format_str.format(
            rnick=network.decode(self.repr_nickname(nickname,
                network.get_channel(event.target())))[0],
            nick=network.decode(nickname)[0],
            event=eventtype,
            arg=arg,
            args=' '.join(arg))

    def handle_private_event(self, bot, event):
        """handle private message (i.e. query)"""
        network = bot.network
        nickname = irclib.nm_to_n(event.source() or b'')
        if network.is_one_of_us(nickname):
            return False
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
        """show who information of the other sides.
        Usage: /msg uniko \who channel
               channel -- channel name (as seen from the user)
        """
        arg = irclib.irc_lower(arg.strip())
        if not self.check_channel(bot, arg):
            return False
        network = bot.network
        channel_obj = network.get_channel(network.decode(arg)[0])
        nickname = irclib.nm_to_n(event.source() or b'')
        if not channel_obj.has_user(nickname):
            return False
        for t_network in self.networks:
            if t_network == network:
                continue
            t_channel = self.channels[t_network]
            t_channel_obj = t_network.get_channel(t_network.encode(t_channel)[0])
            if t_channel_obj is None:
                continue
            count = len(t_channel_obj.users())
            nicklist = t_network.decode(self.repr_nicklist(t_channel_obj))[0]
            msg = "Total {n} in {network}'s {channel}: {nicklist}".format(
                n=count,
                network=t_network.name,
                channel=t_channel,
                nicklist=nicklist)
            bot.push_message(Message(
                command='privmsg',
                arguments=(network.decode(nickname)[0], msg)))
        return True

    def handle_whois(self, bot, event, arg):
        r"""show whois information of the other sides.
        Usage: /msg uniko \whois nickname
               nickname -- nickname (as is)
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
        r"""op all unopped member in the channel.
        Usage: /msg uniko \aop channel
               channel -- channel name (as seen from the user)
        """
        arg = irclib.irc_lower(arg.strip())
        if not self.check_channel(bot, arg):
            return False
        network = bot.network
        nickname = irclib.nm_to_n(event.source() or '')
        # TODO: asynchronous?
        for t_network in self.networks:
            if t_network == network:
                continue
            t_channel = t_network.encode(self.channels[t_network])[0]
            t_channel_obj = t_network.get_channel(t_channel)
            t_bot = t_network.get_oper(t_channel)
            if not t_bot:
                continue
            members = set(t_channel_obj.users())
            members = members.difference(t_channel_obj.opers())
            for _ in util.partition(members.__iter__(), 4): # XXX
                mode_string = b'+' + b'o' * len(_) + b' ' + b' '.join(_)
                mode_string = t_network.decode(mode_string)[0]
                t_bot.push_message(Message(
                    command='mode',
                    arguments=(self.channels[t_network], mode_string)))
            message = t_network.decode(b' '.join(members)[0])
            bot.push_message(Message(
                command='privmsg',
                arguments=(network.decode(nickname)[0], message)))
        return True

    def check_channel(self, bot, channel):
        """check if the channel should be handled by self."""
        channel = bot.network.decode(irclib.irc_lower(channel))[0]
        return channel == self.channels[bot.network]

    def push_message(self, network, message):
        """push message into the buffer.
        Arguments:
        network -- target network
        message -- Message instance
        """
        self.buffers[network].push(message)

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

class UnikoBufferingBot(BufferingBot):
    def __init__(self, network, nickname, realname, reconnection_interval=60,
                 use_ssl=False, buffer_timeout=10.0, test_mode=False):
        self.ext_buffers = set()
        self.buffer_iter = itertools.cycle(self.ext_buffers)
        self.network = network
        self.test_mode = test_mode
        BufferingBot.__init__(self, network.server_list, nickname,
            username=b'uniko', realname=b'Uniko the bot',
            reconnection_interval=reconnection_interval, use_ssl=use_ssl,
            codec=network, buffer_timeout=buffer_timeout, passive=True)

    def __lt__(self, bot):
        return hash(self) < hash(bot)

    def flood_control(self):
        if BufferingBot.flood_control(self):
            return True
        buf = self._get_next_buffer()
        if buf:
            self.pop_buffer(buf)
            return True
        return False

    def push_message(self, message):
        """push message into the buffer.
        Arguments:
        message -- Message instance
        """
        self.buffer.push(message)

    def add_buffer(self, message_buffer):
        self.ext_buffers.add(message_buffer)
        self.buffer_iter = itertools.cycle(self.ext_buffers)

    def remove_buffer(self, message_buffer):
        if message_buffer in self.ext_buffers:
            self.ext_buffers.remove(message_buffer)
            self.buffer_iter = itertools.cycle(self.ext_buffers)

    def _get_next_buffer(self):
        if not self.ext_buffers:
            return None
        return next(self.buffer_iter)

    def process_message(self, message):
        if self.test_mode:
            print(time.strftime('%m %d %H:%M:%S'), self.network.name,
                self.network.decode(self.connection.get_nickname()[0]),
                message.command, *message.arguments)
            if message.command not in ['join']:
                return
        BufferingBot.process_message(self, message)

class UnikoBot():
    def __init__(self, config_file_name):
        self.networks = {}
        self.bots = collections.defaultdict(list)
        self.pipes = []
        self.config_file_name = config_file_name
        self.config_timestamp = self._get_config_time()
        self.version = -1
        self.debug = False
        self.load()

    def _get_config_time(self):
        if not os.access(self.config_file_name, os.F_OK):
            return -1
        return os.stat(self.config_file_name).st_mtime

    def _get_config_data(self):
        if not os.access(self.config_file_name, os.R_OK):
            return None
        try:
            return eval(open(self.config_file_name).read())
        except SyntaxError:
            traceback.print_exc()
        return None

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
            if self._get_config_time() > self.config_timestamp:
                self.reload()

    def load(self):
        data = self._get_config_data()
        if not data:
            return False
        self.version = data['version']
        self.debug = data.get('debug', False)
        self.test_mode = data.get('test', False)
        self.load_network(data['network'])
        self.load_bot(data['bot'])
        self.load_pipe(data['pipe'])
        return True

    def reload(self):
        data = self._get_config_data()
        if not data or self.version >= data['version']:
            return False
        print("reloading...")
        self.version = data['version']
        self.debug = data.get('debug', False)
        self.test_mode = data.get('test', False)
        self.reload_network(data['network'])
        self.reload_bot(data['bot'])
        self.reload_pipe(data['pipe'])
        return True

    def reload_network(self, data):
        pass # TODO

    def load_network(self, data):
        for network_data in data:
            self.networks[network_data['name']] = Network(
                network_data['server'],
                name=network_data['name'],
                encoding=network_data['encoding'],
                use_ssl=network_data.get('use_ssl', False))

    def reload_bot(self, data):
        pass # TODO

    def load_bot(self, data):
        for bot_data in data:
            network = self.networks[bot_data['network']]
            bot = network.add_bot(nickname=bot_data['nickname'],
                test_mode=self.test_mode)
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
                weight=pipe_data.get('weight', 1),
                buffer_timeout=pipe_data.get('buffer_timeout', 10.0))
            for network in pipe_data['network']:
                for bot in self.bots[network]:
                    pipe.attach_handler(bot, self.networks[network])
            self.pipes.append(pipe)
        # TODO: shed

def main():
    profile = None
    if len(sys.argv) > 1:
        profile = sys.argv[1]
    if not profile:
        profile = 'config'
    print("Using profile:", profile)
    root_path = os.path.dirname(os.path.abspath(__file__))
    config_file_name = os.path.join(root_path, profile + '.py')
    uniko = UnikoBot(config_file_name)
    uniko.start()

if __name__ == '__main__':
    main()

