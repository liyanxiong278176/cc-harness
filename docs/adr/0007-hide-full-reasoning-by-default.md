# Hide full model reasoning by default

The normal transcript shows concise activity and tool summaries plus the final answer, while full model reasoning and unabridged tool output require `Ctrl+O` or `--verbose`. This keeps permanent terminal scrollback readable and avoids exposing raw reasoning by default, while retaining an explicit diagnostic path for users who need implementation detail.
