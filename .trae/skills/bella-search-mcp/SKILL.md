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
    "Royal",
    "Marfim",
    "Serenity",
    "Bordô",
    "Vinho",
    "Esmeralda",
    "Preto",
    "Preto Absoluto",
    "Rosa",
    "Marinho",
    "Vermelho",
    "Petróleo",
    "Menta",
    "Musgo",
    "Lavanda",
    "Lilás",
    "Rosa Chá",
    "Branco Óptico",
    "Metal",
    "Off-White",
    "Roxo",
    "Amarelo",
    "Ametista",
    "Dourado",
    "Escarlate",
    "Terracota",
    "Fúcsia",
    "Marsala",
    "Oliva",
    "Prata",
    "Sálvia",
    "Tiffany",
    "Canário",
    "Gelo",
    "Mostarda",
    "Pink",
    "Rubi"
  ],
  "fabrics": [
    "Tule",
    "Chiffon",
    "Cetim",
    "cetim",
    "Paetê",
    "tule",
    "Renda",
    "chiffon",
    "Pedraria",
    "paete",
    "Acetinado",
    "Lantejoulas",
    "renda"
  ],
  "silhouette": [
    "Moderno",
    "Evasê",
    "Clássico",
    "Princesa",
    "Sereia",
    "Romântico",
    "classico",
    "sereia",
    "princesa"
  ],
  "neckline": [
    "Coração",
    "Um ombro só",
    "Decote em V",
    "Ombro a ombro",
    "coracao",
    "Tomara que caia",
    "v",
    "Frente única",
    "ombro-a-ombro",
    "tomara-que-caia",
    "Canoa",
    "Costas abertas",
    "frente-unica"
  ],
  "sleeves": [
    "Sem mangas",
    "Alça fina",
    "Um ombro só",
    "alca-fina",
    "Manga curta",
    "um-ombro-so",
    "Manga longa",
    "manga-longa",
    "manga-curta",
    "sem-manga"
  ],
  "details": [
    "Fenda",
    "Brilho",
    "Drapeado",
    "fenda",
    "Cauda longa",
    "brilho",
    "Transparência",
    "Flor",
    "Laço",
    "Pedraria",
    "drapeado",
    "Bordado",
    "Costas abertas",
    "Camadas",
    "Recortes",
    "Detalhe floral",
    "Flores",
    "Recorte",
    "transparencia",
    "Esvoaçante",
    "Flor no ombro",
    "Mangas caídas",
    "Recortes laterais",
    "Renda",
    "Saia volumosa",
    "Véu",
    "bordado",
    "pedraria"
  ]
}
```
