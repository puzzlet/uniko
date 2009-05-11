#!/usr/bin/env python
# coding:utf-8
import time
from collections import defaultdict
from irclib import irc_lower, is_channel, nm_to_n, parse_channel_modes
from irclib import ServerConnectionError
#from irclib import ServerNotConnectedError
from ircbot import SingleServerIRCBot
from util import *
import config

class Packet():
    def __init__(self, target, message='', timestamp=None):
        self.target = target
        self.message = force_unicode(message)
        self.timestamp = timestamp if timestamp else time.time()

    def __repr__(self):
        return (u'<Packet %s %s %s>' % (
            force_unicode(self.target),
            force_unicode(self.message),
            force_unicode(repr(self.timestamp))
        )).encode('utf-8')

class ConvertingBot(SingleServerIRCBot):
    def __init__(self, server_list, nickname, realname, reconnection_interval=60, channels=[], channel_map={}, encoding_here='', encoding_there='', bot_there=None, use_ssl=False):
        SingleServerIRCBot.__init__(self, server_list, nickname, realname, reconnection_interval)
        self.encoding_here = encoding_here
        self.encoding_there = encoding_there
        self.bot_there = bot_there
        self.autojoin_channels = [self._irc_lower(channel) for channel in channels]
        self.channel_map = {}
        for key, value in dict(channel_map).iteritems():
            key = self._irc_lower(key)
            value = self.bot_there._irc_lower(value)
            self.channel_map[key] = value
        self.use_ssl = use_ssl
        for channel in self.channel_map.keys():
            if type(channel) == unicode:
                channel = channel.encode(self.encoding_here, 'ignore')
            self.autojoin_channels.append(channel)

        self.buffer = []
        self.root_user = []
        self.handlers = {
            'auth':  [self._cmd_auth],
        }

        self.initialized = False
        self.connection.add_global_handler('created', self._on_connected)

    def _connect(self):
        """overrides SingleServerIRCBot._connect()"""
        password = None
        if len(self.server_list[0]) > 2:
            password = self.server_list[0][2]
        try:
            self.connect(self.server_list[0][0],
                         self.server_list[0][1],
                         self._nickname,
                         password,
                         ircname=self._realname,
                         ssl=self.use_ssl)
        except ServerConnectionError:
            pass

    def _on_connected(self, c, e):
        trace('Connected.')
        self.join_channels()
        if self.initialized:
            return
        if c != self.connection:
            return
        self.ircobj.execute_delayed(0, self.stay_alive)
        self.ircobj.execute_delayed(0, self.flood_control)
        self.initialized = True #FIXME: should be not initialized until other side is up
        
    def _on_msg(self, c, e):
        if c != self.connection: return
        
        source = e.source()
        nickname, _, _ = source.partition('!')
        nickname = force_unicode(nickname, self.encoding_here)
        target = e.target()
        msg = e.arguments()[0].decode(self.encoding_here, 'ignore')
        
        if msg.startswith('@'):
            cmd, _, _ = msg.partition(' ')
            if cmd[1:] in self.handlers:
                handlers = self.handlers[cmd[1:]]
                for handler in handlers:
                    handler(self, e, msg)
                    
    def _cmd_auth(self, e, msg):
        # TODO
        cmd, msg = msg.partition(' ')
        if msg == config.PASSWORD:
            self.root_user.append(e.source())
            self.buffer.append(Packet(target=e.source(), message='done.'))

    def _irc_lower(self, s):
        # TODO: prepare python 3.0 as string.translate goes unicode
        s = force_unicode(s).encode(self.encoding_here)
        return irc_lower(s).decoe(self.encoding_here)

    def repr_event(self, e):
        result = [e.source(), e.target(), e.eventtype(), e.arguments()]
        return ' '.join([repr(x) for x in result])

    def relay(self, c, e):
        if c != self.connection: return

        target = e.target()
        if is_channel(target):
            self.relay_channel_event(e)
        else:
            self.process_personal_event(e)

    def relay_channel_event(self, e):
        target = e.target()
        if not is_channel(target):
            raise TypeError
        nickname = nm_to_n(e.source())
        if not nickname:
            trace('Unhandled message: %s' % self.repr_event(e))
            return
        if nickname in [self.connection.nickname]:
            return
        channel = self.channels.get(target, None)
        if not channel:
            mode = ''
        elif channel.is_oper(nickname):
            mode = '@'
        elif channel.is_voiced(nickname):
            mode = '+'
        else:
            mode = ' '

        arg = e.arguments()
        target = self.channel_there(target)
        eventtype = e.eventtype().lower()

        msg = None
        if eventtype in ['privmsg', 'pubmsg']:
            msg = '<%s%s> %s' % (mode, nickname, arg[0])
        elif eventtype in ['privnotice', 'pubnotice']:
            msg = '>%s%s< %s' % (mode, nickname, arg[0])
        elif eventtype in ['join']:
            msg = '! %s %s' % (nickname, eventtype)
        elif eventtype in ['topic'] and len(arg) == 1:
            msg = '! %s %s "%s"' % (nickname, eventtype, arg[0])
        elif eventtype in ['kick']:
            msg = '! %s %s %s (%s)' % (nickname, eventtype, arg[0], arg[1])
        elif eventtype in ['mode']:
            modes = parse_channel_modes(' '.join(arg))
            if any(_[0] != '+' or _[1] not in ['o', 'v'] for _ in modes):
                msg = '! %s %s %s' % (nickname, eventtype, ' '.join(arg))
        elif eventtype in ['part']:
            msg = '! %s %s %s' % (nickname, eventtype, ' '.join(arg))
        elif eventtype in ['action']:
            msg = '\x02* %s\x02 %s' % (nickname, ' '.join(arg))
        else:
            msg = '! %s %s %s' % (nickname, eventtype, repr(arg))
            trace('Unexpected message: %s' % repr(msg))

        if not msg:
            return
        msg = force_unicode(msg, self.encoding_here)
        self.bot_there.buffer.append(Packet(target=target, message=msg))

    def process_personal_event(self, e):
        target = e.target()
        if is_channel(target):
            return
            raise TypeError

        nickname = nm_to_n(e.source() or '')
        if nickname == self.connection.nickname:
            return

        eventtype = e.eventtype().lower()
        if eventtype not in ['privmsg']:
            trace('Unhandled message: %s' % self.repr_event(e))
            return

        cmd, _, arg = e.arguments()[0].partition(' ')
        if not cmd.startswith('\\'):
            trace('Ignored message: %s' % self.repr_event(e))
            return

        msg = ''
        cmd = cmd[1:]
        if cmd == 'who':
            channel_here = self.channels.get(arg, None)
            channel_there = self.bot_there.channels.get(self.channel_there(arg), None)
            if channel_here and channel_there and channel_here.has_user(nickname): # or (not channel_there.is_secret())
            # TODO: what will happen if nickname isn't ascii?
                opers = []
                voiced = []
                others = []
                members = channel_there.users()
                for member in members:
                    if channel_there.is_oper(member):
                        opers.append(member)
                    elif channel_there.is_voiced(member):
                        voiced.append(member)
                    else:
                        others.append(member)
                msg = ' '.join([
                            'Total %d:' % len(members),
                            ' '.join(['@%s' % _ for _ in sorted(opers, key=str.lower)]),
                            ' '.join(['+%s' % _ for _ in sorted(voiced, key=str.lower)]),
                            ' '.join(['%s' % _ for _ in sorted(others, key=str.lower)]),
                        ])
                msg = force_unicode(msg, self.encoding_there)
        elif cmd == 'op':
            pass # TODO
        elif cmd == 'aop':
            channel_here = self.channels.get(arg, None)
            channel_there = self.bot_there.channels.get(self.channel_there(arg), None)
            if channel_here and channel_there and channel_there.is_oper(config.BOT_NAME):
            #if channel_here and channel_there and channel_here.is_oper(nickname) and channel_there.is_oper(config.BOT_NAME):
                members = set(channel_there.users()) - set(channel_there.opers())
                for _ in partition(members.__iter__(), 3):
                    mode_string = '+%s %s' % ('o' * len(_), ' '.join(_))
                    self.bot_there.connection.mode(self.channel_there(arg), mode_string)
                msg = force_unicode(' '.join(members), self.encoding_there)

        if not msg:
            trace('Ignored message: %s' % self.repr_event(e))
            return

        msg = force_unicode(msg, self.encoding_here)
        self.buffer.append(Packet(target=nickname, message=msg))

    def global_relay(self, c, e):
        if c != self.connection: return

        try:
            nickname = nm_to_n(e.source())
        except:
            trace('Unhandled message: %s' % self.repr_event(e))
            return
        target = e.target()
        arg = e.arguments()

        eventtype = e.eventtype().lower()
        for channel_name, channel_obj in self.channels.items():
            if not channel_obj.has_user(nickname):    continue
            if eventtype in ['nick']:
                msg = '*%s %s "%s"' % (nickname, eventtype, target)
            if eventtype in ['quit']:
                msg = '*%s %s %s' % (nickname, eventtype, ' '.join(arg))

            msg = force_unicode(msg, self.encoding_here)
            self.bot_there.buffer.append(Packet(target=channel_name, message=msg))

    def flood_control(self):
        while self.buffer and not self.buffer[0].target:
            self.buffer.pop(0)
        if self.buffer:
            packet = self.buffer[0]
            msg = packet.message
            msg = msg.encode(self.encoding_here, 'xmlcharrefreplace')
            delay = 0.5 + len(msg) / 35.
            if delay > 4:
                delay = 4
            self.ircobj.execute_delayed(delay, self.pop_buffer)
        self.ircobj.execute_delayed(0.1, self.flood_control)

    def pop_buffer(self):
        if self.buffer:
            packet = self.buffer.pop(0)
            if (time.time() - packet.timestamp) > config.PURGE_THRESHOLD:
                self.purge_buffer()
            msg = packet.message
            msg = msg.encode(self.encoding_here, 'xmlcharrefreplace')
            target = self._irc_lower(packet.target)
            try:
                self.connection.privmsg(target, msg)
            except:
                self.buffer.insert(0, packet)

    def stay_alive(self):
        return
        try:
            self.connection.privmsg(self.connection.nickname, '.')
            self.ircobj.execute_delayed(10, self.stay_alive)
        except:
            self.buffer = []
            trace("Connection closed. Reconnecting..")
            self.jump_server()
            self.join_channels()

    def purge_buffer(self):
        line_count = defaultdict(int)
        while self.buffer:
            packet = self.buffer.pop()
            trace('Purging %s' % repr(packet))
            line_count[packet.target] += 1
        for target, n in line_count.iteritems():
            self.buffer.append(
                Packet(
                    target=target,
                    message="-- Message lags over %f seconds. Skipping %d lines.."
                        % (config.PURGE_THRESHOLD, n)
                ))

    def join_channels(self):
        for channel in self.autojoin_channels:
            try:
                self.connection.join(channel)
            except:
                pass

    def channel_there(self, channel):
        try:
            result = force_unicode(channel, self.encoding_here)
            result = self.channel_map[result]
            return result.encode(self.encoding_there)
        except:
            return None

class UnikoBot():
    def __init__(self, nickname='uniko'):
        self.cp949Bot = ConvertingBot(
            config.CP949_SERVER['server'],
            nickname,
            'Uniko-chan',
            reconnection_interval = 600,
            channels = set(config.CP949_SERVER['channel_map'].keys()) | set(config.UTF8_SERVER['channel_map'].values()),
            channel_map = config.CP949_SERVER['channel_map'],
            encoding_here = 'cp949',
            encoding_there = 'utf8',
            use_ssl = config.CP949_SERVER['use_ssl'])
        self.utf8Bot = ConvertingBot(
            config.UTF8_SERVER['server'],
            nickname,
            'Uniko-chan',
            reconnection_interval = 600,
            channels = set(config.UTF8_SERVER['channel_map'].keys()) | set(config.CP949_SERVER['channel_map'].values()),
            channel_map = config.UTF8_SERVER['channel_map'],
            encoding_here = 'utf8',
            encoding_there = 'cp949',
            use_ssl = config.UTF8_SERVER['use_ssl'])

        self.cp949Bot.bot_there = self.utf8Bot
        self.utf8Bot.bot_there = self.cp949Bot

    def set_reader(self, bot):
        # "local" relay, where the target is specified
        bot.connection.add_global_handler('privmsg', bot._on_msg, 0)
        bot.connection.add_global_handler('pubmsg', bot._on_msg, 0)

        bot.connection.add_global_handler('action', bot.relay, 0)
#        bot.connection.add_global_handler('join', bot.relay, 0)
        bot.connection.add_global_handler('kick', bot.relay, 0)
        bot.connection.add_global_handler('mode', bot.relay, 0)
#        bot.connection.add_global_handler('part', bot.relay, 0)
        bot.connection.add_global_handler('privmsg', bot.relay, 0)
        bot.connection.add_global_handler('privnotice', bot.relay, 0)
        bot.connection.add_global_handler('pubmsg', bot.relay, 0)
        bot.connection.add_global_handler('pubnotice', bot.relay, 0)
        bot.connection.add_global_handler('topic', bot.relay, 0)

        # "global" relay, where no target is specified
        # it should be called before each of bot.channels is updated, hence -11
#        bot.connection.add_global_handler('nick', bot.global_relay, -11)
#        bot.connection.add_global_handler('quit', bot.global_relay, -11)

    def start(self):
        self.cp949Bot._connect()
        self.utf8Bot._connect()

        self.set_reader(self.utf8Bot)
        self.set_reader(self.cp949Bot)

        timeout = 0.2
        while True:
            self.utf8Bot.ircobj.process_once(timeout)
            self.cp949Bot.ircobj.process_once(timeout)

bot = UnikoBot(nickname = config.BOT_NAME)
bot.start()

# vim: et ts=4 sts=4 sw=4
