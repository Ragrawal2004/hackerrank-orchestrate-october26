import os

# No test may reach the network. llm.py refuses to build the real SDK transport
# while this is set (tests inject a fake transport instead).
os.environ["GHOST_FORBID_NETWORK"] = "1"
