import imp
import traceback

def load(name):
    try:
        module = __import__('formatter.{}'.format(name),
            fromlist=['format_event'])
    except Exception:
        traceback.print_exc()
        return
    return module.format_event

