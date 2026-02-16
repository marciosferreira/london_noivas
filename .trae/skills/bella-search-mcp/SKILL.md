---
name: "bella-search-mcp"
description: "Runs a local MCP search tool for Bella using structured filters and FAISS similarity. Fill `occasions` from: Noiva, Civil, Madrinha, Mãe dos Noivos, Formatura, Debutante, Gala, Convidada. If user is ambiguous, default to Gala. Use colors, fabrics, silhouette, neckline, sleeves, details and exclude_terms to refine."
---

# Bella Search MCP

Use this skill to run a local Model Context Protocol tool that accepts a structured JSON payload (occasion + facets) and returns the top results from the FAISS index.

## When to Use

Invoke when the user describes a desired dress and you want the LLM to fill structured fields (occasion, colors, fabrics, silhouette, neckline, sleeves, details) and fetch filtered results.

## Input Schema (Exemplo)

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
- Este bloco é apenas exemplo de formato e não influencia a seleção.
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

## Contexto Dinâmico

```json
{
  "occasions": [
    "Madrinha",
    "Gala",
    "Convidada",
    "Formatura",
    "Noiva",
    "Civil",
    "Mãe dos Noivos",
    "Debutante"
  ],
  "colors": [
    "Azul",
    "Verde",
    "Branco",
    "Vermelho/Vinho",
    "Royal",
    "Marfim",
    "Serenity",
    "Bordô",
    "Esmeralda",
    "Preto",
    "Preto Absoluto",
    "Marinho",
    "Rosa",
    "Roxo/Lilás",
    "Petróleo",
    "Musgo",
    "Lavanda",
    "Menta",
    "Rosa Chá",
    "Branco Óptico",
    "Amarelo/Laranja",
    "Cinza/Metal",
    "Ametista",
    "Dourado",
    "Escarlate",
    "Off-White",
    "Terracota",
    "Fúcsia",
    "Oliva",
    "Prata",
    "Rubi",
    "Sálvia",
    "Tiffany",
    "Âmbar",
    "Bege/Nude",
    "Canário",
    "Gelo",
    "Lilás",
    "Mostarda",
    "Pink"
  ],
  "fabrics": [
    "Tule",
    "Cetim",
    "Chiffon",
    "Paetê",
    "Renda",
    "Pedraria",
    "Acetinado",
    "Lantejoulas"
  ],
  "silhouette": [
    "Moderno",
    "Evasê",
    "Clássico",
    "Princesa",
    "Sereia",
    "Romântico"
  ],
  "neckline": [
    "Coração",
    "Um ombro só",
    "Decote em V",
    "Ombro a ombro",
    "Tomara que caia",
    "Frente única",
    "Alça fina",
    "Canoa",
    "Costas abertas"
  ],
  "sleeves": [
    "Sem mangas",
    "Alça fina",
    "Um ombro só",
    "Manga curta",
    "Manga longa"
  ],
  "details": [
    "Fenda",
    "Brilho",
    "Drapeado",
    "Cauda longa",
    "Transparência",
    "Flor",
    "Laço",
    "Bordado",
    "Pedraria",
    "Camadas",
    "Costas abertas",
    "Detalhe floral",
    "Recortes",
    "Flores",
    "Recorte",
    "Véu",
    "Caimento fluido",
    "Capa",
    "Esvoaçante",
    "Flor no ombro",
    "Flores aplicadas",
    "Mangas caídas",
    "Recortes laterais",
    "Renda",
    "Saia fluida",
    "Saia volumosa"
  ]
}
```
