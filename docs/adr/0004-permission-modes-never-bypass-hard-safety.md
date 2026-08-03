# Permission modes never bypass hard safety boundaries

Shift+Tab cycles through `default`, `auto-edit`, and `bypass-prompts`: the first asks according to policy, the second auto-approves project-scoped edits, and the third auto-approves operations that policy would otherwise ask about. No mode may override a hard deny, project path boundary, sandbox restriction, or sensitive-data protection; this preserves fast interactive operation without turning a convenience toggle into a security-disable switch.
