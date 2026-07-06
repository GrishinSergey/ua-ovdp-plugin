# Skills

Each skill gets its own directory here with a `SKILL.md` (required) and optional
`scripts/`, `references/`, `examples/` subdirectories:

```
skills/
└── <skill-name>/
    ├── SKILL.md
    ├── scripts/
    ├── references/
    └── examples/
```

`SKILL.md` needs `name`, `description` (third person, concrete trigger phrases), and
`version` frontmatter. Skills are auto-discovered — no manifest registration needed.

For a user-invoked skill (acts like a slash command), also add `argument-hint` and
`allowed-tools` frontmatter, and write the body as instructions *for Claude*, not
*to the user*.

Before writing a new skill, load the `plugin-dev` plugin's `skill-development` skill
for the authoring conventions (lean SKILL.md, progressive disclosure into
references/examples, imperative form).

No skills exist yet — this project currently only exposes the `ovdp-bonds` MCP server
(see `.mcp.json` / `server.py`). Skills will likely wrap common MCP-tool workflows
(e.g. "find the best current UAH bond under N days to maturity") once that logic is
defined.
