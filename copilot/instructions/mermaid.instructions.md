---
applyTo: "**/*.md"
---

# Mermaid Diagram Conventions

| Use Case | Diagram Type |
|----------|--------------|
| Process flows, workflows | `flowchart` |
| API calls, interactions | `sequenceDiagram` |
| Data models, OOP | `classDiagram` |
| Database schema | `erDiagram` |
| Project timelines | `gantt` |
| System states | `stateDiagram-v2` |

Best practices:
- Use `flowchart` over deprecated `graph`
- Keep node IDs short, labels in brackets
- Max 15-20 nodes per diagram; split if larger
- Use subgraphs to group related elements
