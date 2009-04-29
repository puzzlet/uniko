import time
import itertools
import chardet

def trace(str):
    print '[%s] %s' % (time.strftime('%m %d %H:%M:%S'), str)

def partition(iterable, count):
    try:
        while True:
            result = []
            for i in range(count):
                result.append(next(iterable))
            yield result
    except StopIteration:
        if result:
            return result

def force_unicode(str, encoding=None):
    if type(str) == unicode:
        return str
    if not encoding:
        encoding = chardet.detect(str)['encoding']
    if not encoding:
        print 'Cannot find encoding for %s' % repr(str)
        return '?'
    return str.decode(encoding, 'replace')
