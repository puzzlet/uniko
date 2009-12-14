import time
import itertools

def trace(str):
    print '[%s] %s' % (time.strftime('%m %d %H:%M:%S'), str)

def partition(iterable, count):
    """Returns an iterator of the partition of the *iterable* by *count*.
    This is analogous to the namesake from Mathematica.

    Example:
    >>> [_ for _ in partition([1, 2, 3, 4, 5], 2)]
    [[1, 2], [3, 4], [5]]

    See also:
     * http://bugs.python.org/issue1643
     * http://code.activestate.com/recipes/303060/#c1
    """
    i = iter(iterable)
    try:
        while True:
            result = []
            for _ in range(count):
                result.append(i.next())
            yield result
    except StopIteration:
        if result:
            yield result

def periodic(period):
    """Decorate a class instance method so that the method would be
    periodically executed by irclib framework.
    Raising StopIteration stops periodic execution from inside.
    """
    def decorator(f):
        def new_f(self, *args):
            try:
                f(self, *args)
            except StopIteration:
                return
            finally:
                self.ircobj.execute_delayed(period, new_f, (self,) + args)
        return new_f
    return decorator

