import time
import chardet

def trace(str):
    print '[%s] %s' % (time.strftime('%m %d %H:%M:%S'), str)

def force_unicode(str, encoding=None):
    if type(str) == unicode:
        return str
    if not encoding:
        encoding = chardet.detect(str)['encoding']
    if not encoding:
        print 'Cannot find encoding for %s' % repr(str)
        return '?'
    return str.decode(encoding, 'replace')
