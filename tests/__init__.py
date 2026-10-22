import pathlib
import sys

# The tests import `fakes` as a top-level module, and the skill modules from the skill directory.
sys.path[:0] = [str(pathlib.Path(__file__).parent), str(pathlib.Path(__file__).parents[1])]
