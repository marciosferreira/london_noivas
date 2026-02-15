#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════════╗
║       🤖 AGENTE ReAct MODERNO COM MCP TOOLS (DIDÁTICO - 2025)                  ║
║                      VERSÃO OPENAI (function calling nativo)                   ║
║                                                                                ║
║  Padrões atuais da indústria:                                                  ║
║                                                                                ║
║  ✅ FUNCTION CALLING NATIVO da API OpenAI                                      ║
║     A LLM retorna tool_calls[] estruturados — sem text parsing.                ║
║     Equivalente ao tool_use da Anthropic.                                      ║
║                                                                                ║
║  ✅ MULTI-TURN NATURAL = SCRATCHPAD                                            ║
║     O histórico de mensagens É a memória de trabalho:                          ║
║     user → assistant(tool_calls) → tool(result) → assistant(tool_calls) → ...  ║
║                                                                                ║
║  ✅ COMPRESSÃO DE CONTEXTO COM LLM                                             ║
║     Quando o histórico cresce demais, a LLM gera um resumo.                   ║
║                                                                                ║
║  ✅ MCP VIA STDIO (JSON-RPC 2.0)                                               ║
║     Protocolo padrão para ferramentas de LLM.                                 ║
║                                                                                ║
║  COMPARAÇÃO: OPENAI vs ANTHROPIC (function calling nativo):                    ║
║  ┌──────────────────┬────────────────────────┬──────────────────────────┐      ║
║  │ Aspecto          │ OpenAI                  │ Anthropic                │      ║
║  ├──────────────────┼────────────────────────┼──────────────────────────┤      ║
║  │ Tool schema      │ type:"function",        │ name, input_schema       │      ║
║  │                  │ function:{parameters}   │                          │      ║
║  │ LLM pede tool    │ finish_reason=          │ stop_reason=             │      ║
║  │                  │   "tool_calls"          │   "tool_use"             │      ║
║  │ Dados da tool    │ tool_calls[].function   │ ToolUseBlock             │      ║
║  │                  │   .name, .arguments     │   .name, .input          │      ║
║  │ Resultado        │ role="tool",            │ role="user",             │      ║
║  │                  │ tool_call_id=...        │ type="tool_result"       │      ║
║  │ LLM terminou     │ finish_reason="stop"    │ stop_reason="end_turn"   │      ║
║  │ Args format      │ JSON STRING (precisa    │ DICT nativo              │      ║
║  │                  │   json.loads)           │                          │      ║
║  └──────────────────┴────────────────────────┴──────────────────────────┘      ║
║                                                                                ║
║  Requisitos: pip install langfuse                                              ║
║  Uso: OPENAI_API_KEY=sk-... python react_agent_modern.py                       ║
╚══════════════════════════════════════════════════════════════════════════════════╝

CONCEITOS-CHAVE:

┌──────────────────────┬────────────────────┬────────────────────────────────┐
│ Conceito             │ Onde no código      │ Por que importa                │
├──────────────────────┼────────────────────┼────────────────────────────────┤
│ Function Calling     │ process()          │ API retorna JSON, não texto    │
│ finish_reason        │ process()          │ API diz quando parar           │
│ Multi-turn           │ messages array     │ Histórico = scratchpad         │
│ role="tool"          │ process()          │ Resultados das tools           │
│ tool_call_id         │ process()          │ Vincula resultado → chamada    │
│ MCP Protocol         │ MCPClient          │ Padrão de ferramentas          │
│ LLM Compression      │ _compress()        │ Contexto infinito viável       │
│ Schema conversion    │ mcp_to_openai()    │ Ponte MCP ↔ OpenAI             │
└──────────────────────┴────────────────────┴────────────────────────────────┘

FLUXO DO FUNCTION CALLING NATIVO (OpenAI):

  ANTES (text parsing ReAct):          AGORA (function calling nativo):
  ┌─────────────────────────┐           ┌────────────────────────────────┐
  │ System prompt: 800 tok  │           │ System prompt: 150 tok         │
  │ "Responda no formato:   │           │ "Resolva passo a passo"        │
  │  Thought: ...            │           │ (formato é da API, não nosso!) │
  │  Action: ...             │           └──────────────┬─────────────────┘
  │  Action Input: ..."      │                          │
  └──────────┬──────────────┘                          │
             │                                          │
  ┌──────────▼──────────────┐           ┌──────────────▼─────────────────┐
  │ LLM retorna TEXTO       │           │ LLM retorna OBJETOS            │
  │ "Thought: preciso somar │           │ message.tool_calls = [          │
  │  Action: somar           │           │   {id: "call_abc",             │
  │  Action Input: {a:1,b:2}"│           │    function: {                  │
  └──────────┬──────────────┘           │      name: "somar",            │
             │                           │      arguments: '{"a":1,"b":2}'│
  ┌──────────▼──────────────┐           │    }}]                          │
  │ PARSING COM STRING      │           └──────────────┬─────────────────┘
  │ if "Action:" in text... │                          │
  │ json.loads(...)  ← FRÁGIL│           ┌──────────────▼─────────────────┐
  └─────────────────────────┘           │ json.loads(arguments) ← ÚNICO   │
                                         │ parse necessário, e é confiável│
                                         │ porque a API garante formato   │
                                         └────────────────────────────────┘
"""

import json
import subprocess
import sys
import os
from typing import Optional


# ════════════════════════════════════════════════════════════════════════════
#  PARTE 1: SERVIDOR MCP (Model Context Protocol) via STDIO
#
#  Protocolo padrão para ferramentas de LLM.
#  JSON-RPC 2.0 sobre stdin/stdout do subprocesso.
#  Métodos: initialize → tools/list → tools/call
# ════════════════════════════════════════════════════════════════════════════

MCP_SERVER_CODE = r'''
#!/usr/bin/env python3
"""Servidor MCP (stdio) — ferramentas de soma e subtração."""
import sys, json

def handle(req):
    method, rid, params = req.get("method",""), req.get("id"), req.get("params",{})

    if method == "initialize":
        return {"jsonrpc":"2.0","id":rid,"result":{
            "protocolVersion":"2024-11-05",
            "capabilities":{"tools":{}},
            "serverInfo":{"name":"calculadora-mcp","version":"1.0.0"}
        }}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc":"2.0","id":rid,"result":{"tools":[
            {"name":"somar",
             "description":"Soma dois números (a + b).",
             "inputSchema":{"type":"object","properties":{
                 "a":{"type":"number","description":"Primeiro número"},
                 "b":{"type":"number","description":"Segundo número"}
             },"required":["a","b"]}},
            {"name":"subtrair",
             "description":"Subtrai b de a (a - b).",
             "inputSchema":{"type":"object","properties":{
                 "a":{"type":"number","description":"Minuendo"},
                 "b":{"type":"number","description":"Subtraendo"}
             },"required":["a","b"]}}
        ]}}
    if method == "tools/call":
        name = params.get("name","")
        args = params.get("arguments",{})
        a, b = args.get("a",0), args.get("b",0)
        if name == "somar":
            return {"jsonrpc":"2.0","id":rid,"result":{"content":[
                {"type":"text","text":f"{a} + {b} = {a+b}"}],"isError":False}}
        if name == "subtrair":
            return {"jsonrpc":"2.0","id":rid,"result":{"content":[
                {"type":"text","text":f"{a} - {b} = {a-b}"}],"isError":False}}
        return {"jsonrpc":"2.0","id":rid,"result":{"content":[
            {"type":"text","text":f"Tool '{name}' não encontrada"}],"isError":True}}
    return {"jsonrpc":"2.0","id":rid,"error":{"code":-32601,"message":f"Método: {method}"}}

for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        resp = handle(json.loads(line))
        if resp: print(json.dumps(resp), flush=True)
    except Exception as e:
        print(f"[MCP ERROR] {e}", file=sys.stderr)
'''


# ════════════════════════════════════════════════════════════════════════════
#  PARTE 2: CLIENTE MCP
#
#  Gerencia subprocesso do servidor e comunicação JSON-RPC.
#  Ciclo: start() → initialize() → list_tools() → call_tool() × N → stop()
# ════════════════════════════════════════════════════════════════════════════

class MCPClient:
    """Cliente MCP via STDIO (subprocess)."""

    def __init__(self, server_path: str):
        self.server_path = server_path
        self.process: Optional[subprocess.Popen] = None
        self._req_id = 0

    def start(self):
        print("  🔌 Iniciando servidor MCP...")
        self.process = subprocess.Popen(
            [sys.executable, self.server_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        print(f"  ✅ MCP rodando (PID: {self.process.pid})")

    def _send(self, method: str, params: dict = None) -> Optional[dict]:
        self._req_id += 1
        req = {"jsonrpc": "2.0", "id": self._req_id, "method": method}
        if params:
            req["params"] = params
        self.process.stdin.write(json.dumps(req) + "\n")
        self.process.stdin.flush()
        if method.startswith("notifications/"):
            return None
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("MCP não respondeu")
        return json.loads(line.strip())

    def initialize(self):
        res = self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "react-agent", "version": "2.0.0"},
        })
        self._send("notifications/initialized")
        name = res["result"]["serverInfo"]["name"]
        print(f"  🤝 Handshake OK — servidor: {name}")
        return res

    def list_tools(self) -> list:
        res = self._send("tools/list")
        tools = res["result"]["tools"]
        print(f"  🔧 {len(tools)} tools:")
        for t in tools:
            print(f"     • {t['name']}: {t['description']}")
        return tools

    def call_tool(self, name: str, arguments: dict) -> str:
        res = self._send("tools/call", {"name": name, "arguments": arguments})
        return " ".join(
            c["text"] for c in res["result"]["content"] if c["type"] == "text"
        )

    def stop(self):
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=5)
            print("  🛑 MCP encerrado")


# ════════════════════════════════════════════════════════════════════════════
#  PARTE 3: CONVERSÃO MCP → OPENAI TOOL SCHEMA
#
#  O MCP retorna tools no formato MCP:
#    {"name": "somar", "description": "...", "inputSchema": {...}}
#
#  A API OpenAI espera tools no formato OpenAI:
#    {"type": "function", "function": {"name": "somar", "description": "...",
#     "parameters": {...}}}
#
#  A diferença principal:
#    MCP: inputSchema (camelCase, no nível raiz)
#    OpenAI: parameters (dentro de "function", envelopado em "type":"function")
#
#  Na Anthropic seria: input_schema (snake_case, no nível raiz)
# ════════════════════════════════════════════════════════════════════════════

def mcp_to_openai_tools(mcp_tools: list[dict]) -> list[dict]:
    """
    Converte tools do formato MCP para o formato da API OpenAI.

    MCP:    {"name": "somar", "description": "...", "inputSchema": {...}}
    OpenAI: {"type": "function", "function": {"name": "somar",
             "description": "...", "parameters": {...}}}
    """
    openai_tools = []
    for t in mcp_tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["inputSchema"],  # inputSchema → parameters
            }
        })
    return openai_tools


# ════════════════════════════════════════════════════════════════════════════
#  PARTE 4: FUNÇÕES DE DEBUG / PRINTS DIDÁTICOS
# ════════════════════════════════════════════════════════════════════════════

def print_header(title: str, char: str = "─", width: int = 70):
    print(f"\n  {char*3} {title} {char * max(1, width - len(title) - 5)}")


def print_messages(messages: list[dict], label: str = "MESSAGES"):
    """
    Mostra o array de mensagens de forma legível.

    No formato OpenAI, as mensagens podem ter:
    - role="system"    → system prompt
    - role="user"      → mensagem do usuário
    - role="assistant" → resposta da LLM (pode ter tool_calls)
    - role="tool"      → resultado de uma ferramenta
    """
    print(f"\n  ┌{'─'*68}┐")
    print(f"  │ 📤 {label[:64]:64s}│")
    print(f"  ├{'─'*68}┤")

    for i, msg in enumerate(messages):
        role = msg["role"].upper()
        content = msg.get("content") or ""

        # Mensagem com tool_calls (assistant pedindo tools)
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                fn = tc["function"]
                args_preview = fn["arguments"][:35]
                print(f"  │ [{i:2d}] {role:10s} 🔧 {fn['name']}({args_preview}){' '*15}│")
            # Também pode ter texto
            if content:
                preview = str(content)[:45].replace("\n", " ")
                print(f"  │      {'':10s} 💬 {preview:50s}│")

        # Mensagem role="tool" (resultado)
        elif role == "TOOL":
            tid = msg.get("tool_call_id", "?")[:10]
            preview = str(content)[:40]
            print(f"  │ [{i:2d}] {role:10s} 📥 [{tid}]: {preview:33s}│")

        # Mensagem simples de texto (user, system, assistant sem tools)
        else:
            preview = str(content)[:55].replace("\n", " ↵ ")
            print(f"  │ [{i:2d}] {role:10s} 💬 {preview:50s}│")

    print(f"  └{'─'*68}┘")


def print_api_response(response_data: dict):
    """
    Mostra a resposta da API OpenAI de forma didática.

    Estrutura da resposta OpenAI:
    {
      "choices": [{
        "message": {
          "role": "assistant",
          "content": "texto" | null,
          "tool_calls": [{              ← SÓ EXISTE SE LLM QUER USAR TOOL
            "id": "call_abc123",
            "type": "function",
            "function": {
              "name": "somar",
              "arguments": '{"a":1,"b":2}'   ← JSON STRING, não dict!
            }
          }]
        },
        "finish_reason": "stop" | "tool_calls"
      }],
      "usage": {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}
    }
    """
    choice = response_data["choices"][0]
    message = choice["message"]
    finish = choice["finish_reason"]
    usage = response_data.get("usage", {})

    print(f"\n  ┌{'─'*68}┐")
    print(f"  │ 📥 RESPOSTA DA API OPENAI                                          │")
    print(f"  ├{'─'*68}┤")
    print(f"  │ finish_reason: {finish:51s} │")
    usage_str = f"prompt={usage.get('prompt_tokens',0)} completion={usage.get('completion_tokens',0)} total={usage.get('total_tokens',0)}"
    print(f"  │ usage: {usage_str:60s} │")
    print(f"  ├{'─'*68}┤")

    # Texto da resposta
    content = message.get("content")
    if content:
        for line in content.split("\n")[:5]:
            preview = line[:62]
            print(f"  │ 💬 {preview:64s}│")

    # Tool calls
    tool_calls = message.get("tool_calls")
    if tool_calls:
        for tc in tool_calls:
            fn = tc["function"]
            print(f"  │ 🔧 TOOL_CALL                                                     │")
            print(f"  │    id:        {tc['id']:53s}│")
            print(f"  │    name:      {fn['name']:53s}│")
            print(f"  │    arguments: {fn['arguments'][:53]:53s}│")

    print(f"  └{'─'*68}┘")


# ════════════════════════════════════════════════════════════════════════════
#  PARTE 5: O AGENTE
#
#  ARQUITETURA (OpenAI function calling nativo):
#
#   ┌─────────┐     ┌────────────────┐     ┌──────────────┐
#   │ Usuário │────▶│  process()     │────▶│ OpenAI API   │
#   └─────────┘     │                │     │ tools=[...]  │
#                   │  messages = [  │     └──────┬───────┘
#                   │    system,     │            │
#                   │    user,       │     finish_reason
#                   │    assistant,  │       │
#                   │    tool,       │       ├─ "tool_calls" → executa MCP
#                   │    assistant,  │       │                  appenda msgs
#                   │    tool,       │       │                  volta ao loop
#                   │    ...         │       │
#                   │  ]             │       └─ "stop" → extrai texto
#                   │                │                   retorna
#                   │  O ARRAY       │
#                   │  MESSAGES      │
#                   │  É O           │
#                   │  SCRATCHPAD    │
#                   └────────────────┘
#
#  DIFERENÇA OPENAI vs ANTHROPIC no role de tool results:
#
#  OpenAI:    role="tool", tool_call_id="call_abc"
#  Anthropic: role="user", content=[{type:"tool_result", tool_use_id:"toolu_abc"}]
#
#  Na OpenAI, cada resultado é uma MENSAGEM SEPARADA com role="tool".
#  Na Anthropic, todos os resultados vão em UMA mensagem role="user".
# ════════════════════════════════════════════════════════════════════════════

class Agent:
    """Agente ReAct com function calling nativo da API OpenAI."""

    MAX_ITERATIONS = 15

    # Compressão do turn: muitas tool calls em UMA pergunta
    MAX_TURN_MESSAGES = 24

    # Compressão do histórico: conversa longa entre perguntas
    MAX_HISTORY_MESSAGES = 6

    def __init__(self, api_key: str):
        if sys.version_info >= (3, 14):
            print("⚠️ Langfuse usa Pydantic v1 e não suporta Python 3.14.")
            print("   Use Python 3.13/3.12 para habilitar tracing.")
        try:
            from langfuse.openai import openai as langfuse_openai
            langfuse_openai.api_key = api_key
            self.client = langfuse_openai
        except Exception as exc:
            import openai as openai_module
            print("⚠️ Langfuse OpenAI falhou, usando OpenAI direto.")
            print(f"   Motivo: {exc}")
            openai_module.api_key = api_key
            self.client = openai_module
        self.model = "gpt-4o-mini"

        self.mcp: Optional[MCPClient] = None
        self.tools: list[dict] = []  # Formato OpenAI

        # Histórico persistente entre perguntas
        self.history: list[dict] = []

    # ── Setup ─────────────────────────────────────────────────────────

    def setup(self):
        """Inicia MCP e converte tools para formato OpenAI."""
        print("\n🔧 Setup...")
        path = "/tmp/mcp_calc_server.py"
        with open(path, "w", encoding="utf-8") as f:
            f.write(MCP_SERVER_CODE)

        self.mcp = MCPClient(path)
        self.mcp.start()
        self.mcp.initialize()

        mcp_tools = self.mcp.list_tools()

        # ── CONVERSÃO MCP → OPENAI ──
        self.tools = mcp_to_openai_tools(mcp_tools)

        print("\n  📋 Tools no formato OpenAI:")
        for t in self.tools:
            print(f"     {json.dumps(t, ensure_ascii=False, indent=6)}")
        print()

    # ── System Prompt ─────────────────────────────────────────────────
    #
    # NOTA: Mesmo com OpenAI, o system prompt é simples.
    # As tools são declaradas via parâmetro `tools=` na API,
    # não precisamos descrevê-las no prompt.

    SYSTEM_PROMPT = (
        "Você é um assistente que resolve problemas matemáticos passo a passo.\n\n"
        "Regras:\n"
        "- Use as ferramentas para TODOS os cálculos — nunca calcule mentalmente\n"
        "- Faça UMA operação por vez, usando o resultado da anterior\n"
        "- Explique brevemente seu raciocínio antes de cada operação\n"
        "- Converse em português\n"
        "- Ao terminar, dê a resposta final de forma clara"
    )

    # ── Compressão com LLM ────────────────────────────────────────────

    def _compress_with_llm(self, messages: list[dict]) -> str:
        """
        Usa a própria LLM para comprimir histórico de mensagens.

        QUANDO:
        1. Turn compression: loop de tools de UMA pergunta > MAX_TURN_MESSAGES
        2. History compression: conversa total > MAX_HISTORY_MESSAGES

        POR QUE LLM E NÃO REGRAS:
        - Preserva nuances que regras perdem
        - Sabe quais números/resultados importam
        - Gera resumos que a LLM entende bem depois
        """
        parts = []
        for msg in messages:
            role = msg["role"]
            content = msg.get("content") or ""

            # Mensagem com tool_calls (assistant)
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    fn = tc["function"]
                    parts.append(f"[assistant → tool]: {fn['name']}({fn['arguments']})")
                if content:
                    parts.append(f"[assistant]: {str(content)[:300]}")

            # Resultado de tool
            elif role == "tool":
                parts.append(f"[tool resultado]: {str(content)[:200]}")

            # Mensagem normal
            elif content:
                parts.append(f"[{role}]: {str(content)[:500]}")

        text = "\n".join(parts)
        print(f"\n  📦 COMPRESSÃO COM LLM:")
        print(f"     Input: {len(text)} chars, {len(messages)} msgs")

        try:
            compress_messages = [
                {"role": "system", "content": (
                    "Resuma o histórico abaixo em 2-3 frases objetivas. "
                    "PRESERVE todos os números e resultados. "
                    "Não invente informações."
                )},
                {"role": "user", "content": text},
            ]
            compress_payload = {
                "model": self.model,
                "max_tokens": 300,
                "temperature": 0,
                "messages": compress_messages,
            }
            print("\n===== RAW REQUEST (COMPRESSÃO) BEGIN =====")
            print(json.dumps(compress_payload, ensure_ascii=False))
            print("===== RAW REQUEST (COMPRESSÃO) END =====\n")
            resp = self.client.chat.completions.create(
                model=self.model,
                max_tokens=300,
                temperature=0,
                messages=compress_messages,
            )
            resp_dict = resp.model_dump()
            print("\n===== RAW RESPONSE (COMPRESSÃO) BEGIN =====")
            print(json.dumps(resp_dict, ensure_ascii=False))
            print("===== RAW RESPONSE (COMPRESSÃO) END =====\n")
            summary = resp.choices[0].message.content
            print(f"     Output: {len(summary)} chars")
            print(f"     Resumo: {summary[:150]}...")
            return summary
        except Exception as e:
            print(f"     ❌ Erro: {e}")
            return "Histórico anterior indisponível."

    def _maybe_compress_history(self):
        """Comprime histórico de conversa se ultrapassou limite."""
        if len(self.history) <= self.MAX_HISTORY_MESSAGES:
            return

        print_header("COMPRESSÃO DE HISTÓRICO", "⚡")
        print(f"     Antes: {len(self.history)} msgs")

        keep = 6
        old = self.history[:-keep]
        recent = self.history[-keep:]

        summary = self._compress_with_llm(old)

        self.history = [
            {"role": "user", "content": f"Nossa conversa anterior:\n{summary}"},
            {"role": "assistant", "content": "Compreendo nossa conversa anterior. Como posso continuar ajudando?"},
        ] + recent

        print(f"     Depois: {len(self.history)} msgs")
        print_messages(self.history, "HISTÓRICO APÓS COMPRESSÃO")

    # ── Loop Principal (function calling nativo) ──────────────────────

    def process(self, user_input: str) -> str:
        """
        Processa uma mensagem usando function calling NATIVO da OpenAI.

        ╔════════════════════════════════════════════════════════════════╗
        ║  FLUXO DO FUNCTION CALLING (OpenAI)                           ║
        ║                                                                ║
        ║  1. messages = [system] + history + [user: pergunta]          ║
        ║  2. response = API(messages, tools)                           ║
        ║  3. finish_reason == "stop"?                                  ║
        ║       → extrair content, retornar. FIM.                      ║
        ║  4. finish_reason == "tool_calls"?                            ║
        ║       → para cada tool_call:                                  ║
        ║           → json.loads(arguments)  ← único parse necessário  ║
        ║           → executar via MCP                                  ║
        ║           → messages.append(role="assistant", tool_calls=...) ║
        ║           → messages.append(role="tool", resultado)           ║
        ║       → volta ao passo 2                                     ║
        ║                                                                ║
        ║  O array `messages` cresce — É o scratchpad.                 ║
        ╚════════════════════════════════════════════════════════════════╝
        """
        print(f"\n{'='*70}")
        print(f"  📝 Pergunta: \"{user_input}\"")
        print(f"{'='*70}")

        # Monta mensagens: system + histórico + nova pergunta
        messages = [{"role": "system", "content": self.SYSTEM_PROMPT}]
        messages.extend(self.history)
        messages.append({"role": "user", "content": user_input})

        # Guardar posição inicial para contar msgs do turn
        turn_start = len(messages)

        print_header("MENSAGENS INICIAIS")
        print_messages(messages, "Estado inicial → API")

        final_text = None
        iteration = 0

        for iteration in range(1, self.MAX_ITERATIONS + 1):
            print_header(f"ITERAÇÃO {iteration}/{self.MAX_ITERATIONS}")

            # ── Compressão do turn se necessário ──
            turn_count = len(messages) - turn_start
            print(f"  📊 Mensagens neste turn: {turn_count}")
            print(f"  📊 Mensagens totais: {len(messages)}")

            if turn_count > self.MAX_TURN_MESSAGES:
                print(f"\n  ⚠️  Turn com {turn_count} msgs > limite {self.MAX_TURN_MESSAGES}")

                # Manter: system + history + user original + últimas 4 msgs
                # Comprimir: tudo no meio
                keep_tail = 4
                fixed = messages[:turn_start + 1]  # Até e incluindo user original
                middle = messages[turn_start + 1 : -keep_tail]
                tail = messages[-keep_tail:]

                if middle:
                    summary = self._compress_with_llm(middle)
                    messages = (
                        fixed
                        + [{"role": "assistant", "content": f"[Resumo dos passos anteriores: {summary}]"}]
                        + [{"role": "user", "content": "Continue resolvendo a partir do resumo acima."}]
                        + tail
                    )
                    turn_start = len(fixed) - 1  # Ajusta referência
                    print(f"     Após compressão: {len(messages)} msgs")

            # ── Chama a API OpenAI ──
            print(f"\n  📤 Chamando API OpenAI...")
            print(f"     model: {self.model}")
            print(f"     tools: {[t['function']['name'] for t in self.tools]}")
            print(f"     messages: {len(messages)}")

            payload = {
                "model": self.model,
                "messages": messages,
                "tools": self.tools,
                "temperature": 0.2,
                "max_tokens": 1024,
            }
            print("\n===== RAW REQUEST (OpenAI) BEGIN =====")
            print(json.dumps(payload, ensure_ascii=False))
            print("===== RAW REQUEST (OpenAI) END =====\n")

            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=self.tools,        # ← Tools declaradas nativamente!
                temperature=0.2,
                max_tokens=1024,
            )

            # Converte para dict para debug (o SDK retorna objetos Pydantic)
            response_dict = response.model_dump()
            print("\n===== RAW RESPONSE (OpenAI) BEGIN =====")
            print(json.dumps(response_dict, ensure_ascii=False))
            print("===== RAW RESPONSE (OpenAI) END =====\n")
            print_api_response(response_dict)

            choice = response.choices[0]
            assistant_msg = choice.message
            finish_reason = choice.finish_reason

            # ════════════════════════════════════════════════════════════
            #  DECISÃO: finish_reason
            #
            #  "stop"       → LLM terminou. content = resposta final.
            #  "tool_calls" → LLM quer usar tools. Executar e continuar.
            #
            #  Isso substitui COMPLETAMENTE o text parsing antigo.
            #  Não existe mais "procurar Final Answer: no texto".
            # ════════════════════════════════════════════════════════════

            if finish_reason == "stop":
                # ── RESPOSTA FINAL ──
                print(f"\n  ✅ finish_reason='stop' → Resposta pronta!")
                final_text = assistant_msg.content or ""
                break

            elif finish_reason == "tool_calls":
                # ── EXECUTAR TOOLS ──
                print(f"\n  🔄 finish_reason='tool_calls' → Executando ferramenta(s)...")

                # 1) Append a mensagem do assistant (com tool_calls)
                #
                # NOTA: Na OpenAI, a mensagem do assistant que pede tools
                # deve ser preservada inteira no messages — incluindo os
                # tool_calls — senão a API retorna erro.
                assistant_dict = {
                    "role": "assistant",
                    "content": assistant_msg.content,  # Pode ser None ou texto
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            }
                        }
                        for tc in assistant_msg.tool_calls
                    ]
                }
                messages.append(assistant_dict)

                if assistant_msg.content:
                    print(f"     💬 LLM disse: \"{assistant_msg.content[:80]}\"")

                # 2) Executar cada tool via MCP
                for tc in assistant_msg.tool_calls:
                    fn_name = tc.function.name
                    fn_args_str = tc.function.arguments
                    call_id = tc.id

                    # NOTA: Na OpenAI, arguments é uma JSON STRING, não dict.
                    # Precisamos de json.loads(). Mas isso é seguro porque
                    # a API garante JSON válido (diferente de text parsing).
                    fn_args = json.loads(fn_args_str)

                    print(f"\n     ⚡ MCP call: {fn_name}({json.dumps(fn_args)})")
                    print(f"        call_id: {call_id}")

                    observation = self.mcp.call_tool(fn_name, fn_args)
                    print(f"        👁️ Resultado: {observation}")

                    # 3) Append resultado como role="tool"
                    #
                    # DIFERENÇA vs Anthropic:
                    #   OpenAI:    role="tool", tool_call_id="call_abc"
                    #   Anthropic: role="user", content=[{type:"tool_result"}]
                    #
                    # Na OpenAI, CADA resultado é uma mensagem SEPARADA.
                    # Na Anthropic, todos vão em UMA mensagem.
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": observation,
                    })

                # Debug: estado das mensagens
                print_messages(messages, f"Mensagens após iteração {iteration}")

                continue  # Volta ao loop

            else:
                print(f"  ⚠️ finish_reason inesperado: {finish_reason}")
                final_text = "Erro inesperado."
                break

        if final_text is None:
            final_text = f"Não resolvi em {self.MAX_ITERATIONS} iterações."

        # ── Salvar no histórico (só pergunta + resposta, sem loop) ──
        self.history.append({"role": "user", "content": user_input})
        self.history.append({"role": "assistant", "content": final_text})

        # ── Comprimir histórico se necessário ──
        self._maybe_compress_history()

        # ── Stats ──
        print_header("STATS DO TURN")
        print(f"     Iterações: {iteration}")
        print(f"     Mensagens no turn: {len(messages) - turn_start}")
        print(f"     Histórico persistente: {len(self.history)} msgs")

        return final_text

    # ── Loop interativo ───────────────────────────────────────────────

    def run(self):
        """Loop de conversa contínua."""
        print("""
╔══════════════════════════════════════════════════════════════════╗
║     🤖 AGENTE ReAct MODERNO — OpenAI Function Calling (2025)   ║
║                                                                  ║
║  Exemplos:                                                       ║
║  • "Pegue 100, some 50, tire 30, some 15"                      ║
║  • "Quanto é 42 + 18 - 7 + 100?"                               ║
║  • "Comece com 1000, subtraia 10, some 10"                     ║
║                                                                  ║
║  Comandos: sair | stats | historico | limpar                    ║
╚══════════════════════════════════════════════════════════════════╝
""")
        while True:
            try:
                inp = input("\n🧑 Você: ").strip()
                if not inp:
                    continue
                if inp.lower() == "sair":
                    print("\n👋 Até logo!")
                    break
                if inp.lower() == "stats":
                    n = len(self.history)
                    chars = sum(len(str(m.get("content", ""))) for m in self.history)
                    print(f"\n📊 Histórico: {n} msgs, {chars} chars")
                    continue
                if inp.lower() == "historico":
                    print_messages(self.history, "HISTÓRICO PERSISTENTE")
                    continue
                if inp.lower() == "limpar":
                    self.history.clear()
                    print("\n🧹 Limpo!")
                    continue

                answer = self.process(inp)
                print(f"\n{'─'*70}")
                print(f"  🤖 Agente: {answer}")
                print(f"{'─'*70}")

            except KeyboardInterrupt:
                print("\n\n👋 Até logo!")
                break
            except Exception as e:
                print(f"\n❌ Erro: {e}")
                import traceback
                traceback.print_exc()

    def cleanup(self):
        if self.mcp:
            self.mcp.stop()


# ════════════════════════════════════════════════════════════════════════════
#  PARTE 6: MAIN
# ════════════════════════════════════════════════════════════════════════════

def _load_dotenv():
    """Carrega variáveis de .env se existir."""
    if os.environ.get("RENDER") or os.environ.get("RENDER_EXTERNAL_URL"):
        return
    for base in [os.getcwd(), os.path.dirname(os.path.abspath(__file__))]:
        envpath = os.path.join(base, ".env")
        if not os.path.isfile(envpath):
            continue
        with open(envpath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.lower().startswith("export "):
                    line = line[7:].strip()
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if " #" in val:
                    val = val.split(" #", 1)[0].rstrip()
                if " \t#" in val:
                    val = val.split("\t#", 1)[0].rstrip()
                val = val.strip().strip("'\"`")
                if key and val and key not in os.environ:
                    os.environ[key] = val
        print(f"  📄 .env carregado: {envpath}")
        break


def _sanitize_env_value(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return value.strip().strip("'\"`")


def _normalize_langfuse_env():
    public_key = _sanitize_env_value(os.environ.get("LANGFUSE_PUBLIC_KEY"))
    secret_key = _sanitize_env_value(os.environ.get("LANGFUSE_SECRET_KEY"))
    base_url = _sanitize_env_value(
        os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST")
    )
    if base_url:
        base_url = base_url.rstrip("/")
    if public_key:
        os.environ["LANGFUSE_PUBLIC_KEY"] = public_key
    if secret_key:
        os.environ["LANGFUSE_SECRET_KEY"] = secret_key
    if base_url:
        os.environ["LANGFUSE_BASE_URL"] = base_url
        os.environ.setdefault("LANGFUSE_HOST", base_url)


def main():
    print("""
    ╔═══════════════════════════════════════════════════════════════════╗
    ║    🧠 AGENTE ReAct MODERNO — OpenAI Function Calling (2025)      ║
    ║                                                                   ║
    ║  Arquitetura:                                                     ║
    ║                                                                   ║
    ║  OpenAI API ◄──── function calling nativo (sem text parsing)     ║
    ║       │            multi-turn natural (messages = scratchpad)     ║
    ║       │            finish_reason guia o loop                     ║
    ║       ▼                                                           ║
    ║  Agent ◄────► MCP Client ◄────► MCP Server (subprocess)         ║
    ║    │                              • somar(a, b)                  ║
    ║    │                              • subtrair(a, b)               ║
    ║    ▼                                                              ║
    ║  LLM Compression (quando contexto > limite)                      ║
    ╚═══════════════════════════════════════════════════════════════════╝
    """)

    _load_dotenv()
    _normalize_langfuse_env()
    print("\n  🔍 ENV Langfuse:")
    print(f"     LANGFUSE_PUBLIC_KEY: {'ok' if os.environ.get('LANGFUSE_PUBLIC_KEY') else 'missing'}")
    print(f"     LANGFUSE_SECRET_KEY: {'ok' if os.environ.get('LANGFUSE_SECRET_KEY') else 'missing'}")
    print(f"     LANGFUSE_BASE_URL: {os.environ.get('LANGFUSE_BASE_URL', 'missing')}")

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("❌ Configure OPENAI_API_KEY")
        print("   export OPENAI_API_KEY='sk-...'")
        print("\n   Ou cole aqui (Enter para cancelar):")
        api_key = input("   Key: ").strip()
        if not api_key:
            return

    agent = Agent(api_key)
    try:
        agent.setup()
        agent.run()
    finally:
        agent.cleanup()


if __name__ == "__main__":
    main()
