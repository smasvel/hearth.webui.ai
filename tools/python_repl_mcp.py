"""Persistent local Python tool for Hearth's existing MCP client."""
import ast
import contextlib
import io
import traceback

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Hearth Python")
namespace = {"__name__": "__hearth_repl__"}


@mcp.tool()
def execute_python(code: str) -> str:
    """Execute Python locally, retaining variables between calls; return output."""
    output = io.StringIO()
    try:
        tree = ast.parse(code)
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                expression = tree.body.pop().value
                exec(compile(tree, "<hearth>", "exec"), namespace)
                result = eval(compile(ast.Expression(expression), "<hearth>", "eval"), namespace)
                if result is not None:
                    print(repr(result))
            else:
                exec(compile(tree, "<hearth>", "exec"), namespace)
    except Exception:
        output.write(traceback.format_exc())
    return output.getvalue()[:100000]


if __name__ == "__main__":
    mcp.run(transport="stdio")
