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
    "Convidada",
    "Madrinha",
    "Gala",
    "Formatura",
    "Noiva",
    "Civil",
    "Mãe dos Noivos",
    "Debutante",
    "Mae dos Noivos"
  ],
  "colors": [
    "Verde esmeralda",
    "Azul royal",
    "Off-white",
    "Azul claro",
    "Vermelho",
    "Preto",
    "Rosa",
    "Branco",
    "Verde",
    "Azul marinho",
    "Lilás",
    "Verde menta",
    "Vinho",
    "Bordô",
    "Dourado",
    "Marrom",
    "Roxo",
    "Verde musgo",
    "Verde oliva",
    "Amarelo",
    "Prata",
    "Azul",
    "Azul petróleo",
    "Champagne",
    "Laranja",
    "Magenta",
    "Terracota",
    "Verde escuro"
  ],
  "fabrics": [
    "Tule",
    "Cetim",
    "Chiffon",
    "Renda",
    "Lantejoulas",
    "Paetê",
    "Brilho",
    "Crepe",
    "Paetês",
    "Pedraria",
    "Veludo",
    "renda",
    "tule"
  ],
  "silhouette": [
    "Evasê",
    "Moderno",
    "Sereia",
    "Clássico",
    "Princesa",
    "Reto",
    "Romântico",
    "Fluido",
    "sereia"
  ],
  "neckline": [
    "Decote em V",
    "Ombro a ombro",
    "Assimétrico",
    "Tomara que caia",
    "Um ombro só",
    "Coração",
    "Frente única",
    "Decote coração",
    "Um ombro",
    "Reto",
    "Alça fina",
    "Costas abertas",
    "Decote em coração",
    "Decote quadrado",
    "Drapeado",
    "Franzido",
    "v"
  ],
  "sleeves": [
    "Sem mangas",
    "Alça fina",
    "Manga curta",
    "Manga longa",
    "Um ombro só",
    "Alça única",
    "Ombro a ombro",
    "Alça larga",
    "Manga caída",
    "Um ombro",
    "Uma manga",
    "Capa",
    "Manga única",
    "Uma alça"
  ],
  "details": [
    "Fenda",
    "Brilho",
    "Drapeado",
    "Detalhe floral",
    "Cauda longa",
    "Laço",
    "Pedraria",
    "Camadas",
    "Recortes",
    "Renda",
    "Cauda",
    "Costas abertas",
    "Esvoaçante",
    "Flor",
    "Fluido",
    "Recorte",
    "Caimento fluido",
    "Capa",
    "Cintura marcada",
    "Flor no ombro",
    "Flores",
    "Transparência",
    "Véu",
    "Alça longa",
    "Amarração",
    "Aplicação de folhas",
    "Aplicações florais",
    "Babado",
    "Camadas assimétricas",
    "Cinto",
    "Corset",
    "Detalhes na cintura",
    "Flores aplicadas",
    "Laço no ombro",
    "Recorte frontal",
    "Saia esvoaçante",
    "Saia volumosa",
    "Sobreposição",
    "Tule transparente",
    "Véu longo"
  ]
}
```
