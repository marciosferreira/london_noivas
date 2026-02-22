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
    "Mãe dos Noivos",
    "Civil",
    "Debutante"
  ],
  "colors": [
    "Azul",
    "Verde",
    "Branco",
    "Royal",
    "Serenity",
    "Bordô",
    "Vinho",
    "Esmeralda",
    "Branco Óptico",
    "Preto",
    "Preto Absoluto",
    "Rosa",
    "Marfim",
    "Marinho",
    "Petróleo",
    "Vermelho",
    "Menta",
    "Musgo",
    "Lavanda",
    "Lilás",
    "Off-White",
    "Rosa Chá",
    "Metal",
    "Roxo",
    "Amarelo",
    "Ametista",
    "Dourado",
    "Escarlate",
    "Fúcsia",
    "Marsala",
    "Oliva",
    "Prata",
    "Sálvia",
    "Terracota",
    "Tiffany",
    "Bege",
    "Canário",
    "Champagne",
    "Chocolate",
    "Marrom"
  ],
  "fabrics": [
    "Tule",
    "Chiffon",
    "Cetim",
    "cetim",
    "tule",
    "Paetê",
    "chiffon",
    "Renda",
    "renda",
    "Pedraria",
    "paete",
    "Lantejoulas"
  ],
  "silhouette": [
    "Moderno",
    "Evasê",
    "Clássico",
    "classico",
    "Sereia",
    "Princesa",
    "princesa",
    "Romântico",
    "sereia",
    "evase",
    "minimalista",
    "reto"
  ],
  "neckline": [
    "Coração",
    "Um ombro só",
    "Decote em V",
    "coracao",
    "Ombro a ombro",
    "v",
    "Frente única",
    "tomara-que-caia",
    "Tomara que caia",
    "ombro-a-ombro",
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
    "sem-manga",
    "manga-curta"
  ],
  "details": [
    "Fenda",
    "Brilho",
    "Drapeado",
    "fenda",
    "brilho",
    "Transparência",
    "drapeado",
    "cauda-longa",
    "Cauda longa",
    "Flor",
    "Laço",
    "Bordado",
    "Pedraria",
    "Recortes",
    "pedraria",
    "Camadas",
    "Costas abertas",
    "Detalhe floral",
    "Flores",
    "Recorte",
    "bordado",
    "costas-abertas",
    "transparencia",
    "Esvoaçante",
    "Flor no ombro",
    "Mangas caídas",
    "Recortes laterais",
    "Véu"
  ]
}
```
