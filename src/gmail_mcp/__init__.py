"""First-party Gmail MCP servers (Milestone 18).

One package, three stdio server modes — ``read``, ``write``, ``send`` — each
serving one operator-classified roster over the owner's Gmail mailbox. The
package is deliberately independent of ``agent_core``: it reaches the
platform only through the MCP protocol, and a structural gate walks the
import graph in both directions.
"""
