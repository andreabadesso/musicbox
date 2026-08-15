"""musicbox: a thin, event shaped HTTP API in front of Music Assistant.

Music Assistant (MA) does the heavy lifting: providers, queue, and the built in
snapserver. musicbox only adds the endpoints the rest of the system talks to,
and it is deliberately the only thing that system needs to know about.
"""

__version__ = "0.1.0"
