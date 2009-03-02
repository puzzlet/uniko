#!/usr/bin/env python
# coding:utf-8
import time
from collections import defaultdict
from irclib import is_channel, ServerConnectionError
#from irclib import ServerNotConnectedError
from ircbot import SingleServerIRCBot as Bot
from util import *
import config

class Packet():
    def __init__(self, target, message='', timestamp=None):
        self.target = target
        self.message = message
        self.timestamp = timestamp if timestamp else time.time()

    def __repr__(self):
        return '<Packet %s %s %s>' % (self.target, self.message, repr(self.timestamp))

class ConvertingBot(Bot):
    def __init__(self, server_list, nickname, realname, reconnection_interval=60, channels=[], channel_map={}, encoding_here='', encoding_there='', bot_there=None, use_ssl=False):
        Bot.__init__(self, server_list, nickname, realname, reconnection_interval)
        self.encoding_here = encoding_here
        self.encoding_there = encoding_there
        self.bot_there = bot_there
        self.autojoin_channels = list(channels)
        self.channel_map = dict(channel_map)
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

        #XXX: should be not initialized until other side is up
        self.initialized = False

        # "local" relay, where the target is specified
        self.connection.add_global_handler('created', self._on_connected)
        self.connection.add_global_handler('privmsg', self._on_msg, 0)
        self.connection.add_global_handler('pubmsg', self._on_msg, 0)

        self.connection.add_global_handler('action', self.relay, 0)
#        self.connection.add_global_handler('join', self.relay, 0)
        self.connection.add_global_handler('kick', self.relay, 0)
        self.connection.add_global_handler('mode', self.relay, 0)
#        self.connection.add_global_handler('part', self.relay, 0)
        self.connection.add_global_handler('privmsg', self.relay, 0)
        self.connection.add_global_handler('privnotice', self.relay, 0)
        self.connection.add_global_handler('pubmsg', self.relay, 0)
        self.connection.add_global_handler('pubnotice', self.relay, 0)
        self.connection.add_global_handler('topic', self.relay, 0)

        # "global" relay, where no target is specified
        # it should be called before each of self.channels is updated, hence -11
#        self.connection.add_global_handler('nick', self.global_relay, -11)
#        self.connection.add_global_handler('quit', self.global_relay, -11)

    def _connect(self):
        """overrides Bot._connect()"""
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
        if self.initialized:    return
        if c != self.connection:    return
        self.ircobj.execute_delayed(0, self.stay_alive)
        self.ircobj.execute_delayed(0, self.flood_control)
        self.initialized = True
        
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
    
    def repr_event(self, e):
        result = [e.source(), e.target(), e.eventtype(), e.arguments()]
        return ' '.join([repr(x) for x in result])

    def get_nickname(self, e):
        source = e.source()
        if not source:
            return None, None
        nickname, _, _ = source.partition('!')

        target = e.target()
        channel = self.channels.get(target, None)
        if not channel:
            return '', nickname

        if channel.is_oper(nickname):
            mode = '@'
        elif channel.is_voiced(nickname):
            mode = '+'
        else:
            mode = ' '

        return mode, nickname

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
        mode, nickname = self.get_nickname(e)
        if not nickname:
            trace('Unhandled message: %s' % self.repr_event(e))
            return
        if nickname == config.BOT_NAME: # XXX
            return

        arg = e.arguments()
        target = self.channel_there(target)
        eventtype = e.eventtype().lower()

        msg = None
        if eventtype in ['privmsg', 'pubmsg']:
            msg = '<%s%s> %s' % (mode, nickname, arg[0])
        elif eventtype in ['privnotice', 'pubnotice']:
            msg = '>%s%s< %s' % (mode, nickname, arg[0])
        elif eventtype in ['join']:
            msg = '*%s %s' % (nickname, eventtype)
        elif eventtype in ['topic'] and len(arg) == 1:
            msg = '*%s %s "%s"' % (nickname, eventtype, arg[0])
        elif eventtype in ['kick']:
            msg = '*%s %s %s (%s)' % (nickname, eventtype, arg[0], arg[1])
        elif eventtype in ['mode']:
            if not (arg and arg[0].startswith('+o')):
                msg = '*%s %s %s' % (nickname, eventtype, ' '.join(arg))
        elif eventtype in ['part']:
            msg = '*%s %s %s' % (nickname, eventtype, ' '.join(arg))
        elif eventtype in ['action']:
            msg = '\001ACTION <%s%s> %s\001' % (mode, nickname, ' '.join(arg))
        else:
            msg = '*%s %s %s' % (nickname, eventtype, repr(arg))
            trace('Unexpected message: %s' % repr(msg))

        if msg:
            msg = force_unicode(msg, self.encoding_here)
            self.bot_there.buffer.append(Packet(target=target, message=msg))

    def process_personal_event(self, e):
        target = e.target()
        if is_channel(target):
            return
            raise TypeError

        mode, nickname = self.get_nickname(e)
        if nickname == config.BOT_NAME: # XXX
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
                members = []
                for member in channel_there.users():
                    if channel_there.is_oper(member):
                        members.append('@%s' % member)
                    elif channel_there.is_voiced(member):
                        members.append('+%s' % member)
                    else:
                        members.append(member)
                msg = force_unicode(' '.join(members), self.encoding_there)
        elif cmd == 'op':
            pass # TODO
        elif cmd == 'aop':
            channel_here = self.channels.get(arg, None)
            channel_there = self.bot_there.channels.get(self.channel_there(arg), None)
            if channel_here and channel_there and channel_there.is_oper(config.BOT_NAME):
            #if channel_here and channel_there and channel_here.is_oper(nickname) and channel_there.is_oper(config.BOT_NAME):
                members = set(channel_there.users()) - set(channel_there.opers())
                for _ in members:
                    self.bot_there.connection.mode(self.channel_there(arg), '+o %s' % _)
                msg = force_unicode(' '.join(members), self.encoding_there)

        if not msg:
            trace('Ignored message: %s' % self.repr_event(e))
            return

        msg = force_unicode(msg, self.encoding_here)
        self.buffer.append(Packet(target=nickname, message=msg))

    def global_relay(self, c, e):
        if c != self.connection: return

        try:
            mode, nickname = self.get_nickname(e)
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
            msg = force_unicode(packet.message)
            msg = msg.encode(self.encoding_here, 'xmlcharrefreplace')
            delay = 0.5 + len(msg) / 35.
            if delay > 4:
                delay = 4
            self.ircobj.execute_delayed(delay, self.pop_buffer)
        self.ircobj.execute_delayed(0.1, self.flood_control)

    def pop_buffer(self):
        if self.buffer:
            packet = self.buffer.pop(0)
            if (time.time() - packet.timestamp) > config.RESPAWN_THRESHOLD:
                self.respawn()
            msg = force_unicode(packet.message)
            msg = msg.encode(self.encoding_here, 'xmlcharrefreplace')
            try:
                self.connection.privmsg(packet.target, msg)
            except:
                self.buffer.insert(0, packet)

    def stay_alive(self):
        try:
            self.connection.privmsg(config.BOT_NAME, '.') # XXX
            self.ircobj.execute_delayed(10, self.stay_alive)
        except:
            self.buffer = []
            trace("Connection closed. Reconnecting..")
            self.jump_server()
            self.join_channels()

    def respawn(self):
        trace("Skipping..")
        line_count = defaultdict(int)
        while self.buffer:
            packet = self.buffer.pop()
            line_count[packet.target] += 1
        for target, n in line_count.iteritems():
            self.buffer.append(
                Packet(
                    target=target,
                    message="-- Message lags over %f seconds. Skipping %d lines.."
                        % (config.RESPAWN_THRESHOLD, n)
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
            encoding_there = 'utf8')
        self.utf8Bot = ConvertingBot(
            config.UTF8_SERVER['server'],
            nickname, 'Uniko-chan',
            reconnection_interval = 600,
            channels = set(config.UTF8_SERVER['channel_map'].keys()) | set(config.CP949_SERVER['channel_map'].values()),
            channel_map = config.UTF8_SERVER['channel_map'],
            encoding_here = 'utf8',
            encoding_there = 'cp949',
            use_ssl = True)
        self.cp949Bot.bot_there = self.utf8Bot
        self.utf8Bot.bot_there = self.cp949Bot

    def start(self):
        self.utf8Bot._connect()
        self.cp949Bot._connect()

        timeout = 0.2
        while True:
            self.utf8Bot.ircobj.process_once(timeout)
            self.cp949Bot.ircobj.process_once(timeout)

bot = UnikoBot(nickname = config.BOT_NAME)
bot.start()

# vim: et ts=4 sts=4 sw=4
