"""blendahbot — an autonomous agent that builds in Blender until the result matches your request.

The bot drives Claude (via the Claude Agent SDK, reusing your Claude Code login) in a
loop: plan -> build in Blender -> render -> have an independent critic judge the render
against your request -> revise -> repeat, until the result is genuinely good or limits
are hit. It can also browse the web, download assets, run shell commands and install
tools to maximise the request.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
