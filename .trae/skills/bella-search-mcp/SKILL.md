---
name: "bella-search-mcp"
description: "Runs a local MCP search tool for Bella using structured filters and FAISS similarity. Invoke when you need the LLM to fill occasion and facet fields for catalog search."
---

# Bella Search MCP

Use this skill to run a local Model Context Protocol tool that accepts a structured JSON payload (occasion + facets) and returns the top results from the FAISS index.

## When to Use

Invoke when the user describes a desired dress and you want the LLM to fill structured fields (occasion, colors, fabrics, silhouette, neckline, sleeves, details) and fetch filtered results.

## Input Schema

```json
{
  "query": "string, optional natural language query",
  "k": 4,
  "occasions": ["Mãe dos Noivos", "Gala"],
  "colors": ["Vermelho"],
  "fabrics": ["Renda", "Tule"],
  "silhouette": ["Sereia"],
  "neckline": ["Decote em V"],
  "sleeves": ["Manga longa"],
  "details": ["Cauda longa"],
  "exclude_terms": ["sem brilho"]
}
```

Notes:
- Any field can be a single string or a list.
- If `query` is omitted, the tool will build one from the filters.
- `k` defaults to 4 when not provided.

## Output Schema

```json
{
  "suggestions": [
    {
      "custom_id": "string",
      "title": "string",
      "description": "string",
      "file_name": "string",
      "metadata_filters": { "colors": [], "fabrics": [], "occasions": [] }
    }
  ]
}
```

## Local Tool

Run the local tool with JSON on stdin:

```bash
python .trae/skills/bella-search-mcp/main.py < payload.json
```
