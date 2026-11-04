# No test may reach a real hopper-notify on the machine running the suite.
from hopper import util

REAL_RUN_NOTIFIER = util._run_notifier
util._run_notifier = lambda *args: None
