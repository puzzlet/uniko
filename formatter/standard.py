import irclib

def safe_decode(string, encoding):
    return string.decode(encoding, 'ignore')

def repr_nickname(nickname, channel):
    """format nickname according to its mode given in the channel.
    Arguments:
    nickname -- nickname in bytes
    channel -- ircbot.Channel instance
    """
    assert isinstance(nickname, bytes)
    if not channel:
        return nickname
    # TODO: halfop and all the other modes
    elif channel.is_oper(nickname):
        return b'@' + nickname
    elif channel.is_voiced(nickname):
        return b'+' + nickname
    return b' ' + nickname

def format_event(event, channel, encoding):
    eventtype = event.eventtype().lower()
    nickname = irclib.nm_to_n(event.source() or '')
    arg = [safe_decode(_, encoding) for _ in event.arguments()]
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
        rnick=safe_decode(repr_nickname(nickname, channel), encoding),
        nick=safe_decode(nickname, encoding),
        event=eventtype,
        arg=arg,
        args=' '.join(arg))

