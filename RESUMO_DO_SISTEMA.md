# Visão Geral do Sistema

- Aplicação de aluguel com três entidades principais: `item`, `client` e `transaction`.
- Persistência em DynamoDB; armazenamento de imagens em S3; envio de e-mails via AWS SES.
- Integração de cobrança com Stripe (customer, subscription, webhooks).
- Uso de `session` para autenticação, autorização e contexto de navegação/paginação.

## Dados e Tabelas

- `users_table`: dados do usuário (e-mail, username, `role`, `status`, `timezone`, `stripe_customer_id`).
- `accounts_table`: plano da conta (`free`|`business`) e `payment_status`.
- `itens_table`: inventário de itens; campos fixos e personalizados via `key_values`.
- `clients_table`: clientes; campos fixos e personalizados via `key_values`.
- `transactions_table`: transações de aluguel (datas, valores, status, vínculo com item e cliente).
- `text_models_table`: modelos de texto para visualização/impressão.
- `payment_transactions_table`: status de assinatura e pagamentos do Stripe.

## Autenticação e Administração

- `register` + `create_user`:
  - Cria `customer` e `subscription` no Stripe com `trial_period_days`.
  - Persiste usuário (`password_hash`, `email_token`) e envia e-mail de confirmação.
- `login`: autentica e preenche `session` (`user_id`, `account_id`, `role`, etc.).
- `logout`: limpa sessão e redireciona.
- `adjustments`: exibe cobrança atual do Stripe, cartão salvo, timezone e histórico.
- `admin_dashboard`: lista usuários admins com paginação e métricas agregadas.
- Impersonação: `login-as-user/<user_id>` e `return-to-admin` para suporte/admin.
- `utils.get_user_timezone(users_table)`: retorna timezone do usuário (fallback `America/Sao_Paulo`).

## Itens e Imagens

- Uploads:
  - `item_routes.allowed_file` e `item_routes.handle_image_upload` (rotas): valida extensão e salva/substitui arquivo.
  - `utils.upload_image_to_s3` e `utils.copy_image_in_s3`: upload/cópia de imagens no S3 com chaves seguras.
- Rotas de item:
  - `/add_item` (GET/POST): cria item; valida `item_custom_id` único; sanitiza números/CPF/CNPJ/telefone; salva campos fixos e `key_values`.
  - `/edit_item/<item_id>` (GET/POST): edita item; pode excluir/substituir imagem; valida; atualiza campos; opcionalmente reflete em transações.

## Transações

- `/rent` (GET/POST): fluxo de aluguel com blocos (item, cliente, transação) controlados por query:
  - Cria/atualiza cliente e item se necessário.
  - Valida período de aluguel e campos numéricos/identificação.
  - Persiste transação em `transactions_table`.
- `/edit_transaction/<transaction_id>` (GET/POST): edita transação, carrega item/cliente associados, valida período, atualiza campos fixos e `key_values` e gerencia reservas de datas do item.
- `/delete_transaction/<transaction_id>`: soft delete com `status=deleted`, `deleted_date`, `deleted_by` e `previous_status`.

## Listagens, Filtros e Paginação

- Listas principais: `/rented`, `/reserved`, `/returned`, `/archive`, `/trash_itens`, `/trash_transactions`, `/inventory`, `/all_transactions`.
- `item_routes.list_transactions` e `item_routes.list_raw_itens`:
  - Paginação via `ExclusiveStartKey` e cursores em `session`.
  - Cálculo de indicadores (ex.: atraso/overdue, mensagens de aluguel).
  - Aplicação de filtros dinâmicos em campos fixos/personalizados.
- `utils.entidade_atende_filtros_dinamico(item, filtros, fields_config, image_url_required)`:
  - Tipos: texto (contains, case-insensitive), números/valores (min/max), opções (igualdade), datas (intervalos `YYYY-MM-DD`).
  - Datas fixas: `rental_date`, `return_date` e `created_at` com validação robusta.
  - Filtro de imagem: exige ou não presença de `item_image_url`.
- `utils.converter_intervalo_data_br_para_iso(filtros, chave, destino_inicio, destino_fim)`:
  - Converte `dd/mm/yyyy - dd/mm/yyyy` para `yyyy-mm-dd` em dois destinos.
- Auxiliares:
  - `item_routes.apply_filtros_request`: extrai filtros do `request.args`.

## Mudança de Status

- `/mark_rented/<transaction_id>`: define `status=rented`, `rental_date` e, se `pago_total`, atualiza `transaction_value_paid`.
- `/mark_returned/<transaction_id>`: define `status=returned` e `return_date`.
- `/mark_archived/<item_id>` e `/mark_available/<item_id>`: alternam status do item.
- Todas exigem autenticação e refletem nas listagens.

## Modelos, Impressão e QR Code

- `/modelos`: lista modelos (do usuário e exemplos). Requer login.
- `/criar-modelo`: cria modelo e salva em `text_models_table`.
- `/visualizar-modelo/<text_id>/<transacao_id>`: renderiza modelo com dados da transação (datas formatadas, substituições dinâmicas).
- `/visualizar-modelo/<text_id>`: renderiza com dados de exemplo.
- `/editar-modelo/<text_id>` e `/excluir-modelo/<text_id>`: edição/remoção com checagens de propriedade; modelos de exemplo não editáveis.
- `/imprimir-modelo/<item_id>/<modelo_id>`: renderiza modelo com dados do item para impressão.
- `/qr_data/<item_id>`: retorna JSON com `item_custom_id`, `description`, `item_obs`, `image_url`.
- `/imprimir_qrcode/<item_id>`: gera QR para página pública/inventário, opcionalmente com detalhes do item.

## Stripe e Webhooks

- `/webhook/stripe`:
  - Eventos suportados: `checkout.session.completed`, `customer.subscription.created/updated/deleted`, `invoice.payment_failed/paid`.
  - Atualiza `payment_transactions_table` com status de assinatura e pagamentos.
- `item_routes.get_latest_transaction(user_id)`: obtém transação Stripe recente válida para exibir no `adjustments`.

## Interligações Principais

- Autenticação governa acesso (sessão: `logged_in`, `user_id`, `role`).
- Campos por entidade vêm de schemas estáticos (`schemas.get_schema_fields`) e `key_values`.
- Listagens refletem mudanças de status imediatamente e suportam filtros ricos.
- Uploads de imagem enriquecem itens e permitem filtro por presença de imagem.
- Stripe integra cadastro, ajustes e webhooks; `accounts_table.get_account_plan` diferencia recursos (`free` vs `business`).

## Fluxos Comuns

- Criar Item: `/add_item` → validações + upload → salva em `itens_table` → aparece em `/inventory`.
- Criar Aluguel: `/rent` → cria/seleciona cliente e item → valida período/valores → salva em `transactions_table` → aparece em `/reserved`/`/rented`.
- Devolver Item: `/mark_returned/<transaction_id>` → atualiza status/datas → some de `/rented` → aparece em `/returned`.
- Editar Transação: `/edit_transaction/<id>` → valida período → atualiza transação e reservas do item.

---

> Este documento resume as principais rotas, utilitários e interações entre módulos (`item_routes.py`, `static_routes.py`, `status_routes.py`, `transaction_routes.py`, `auth.py`, `utils.py`) para referência rápida da arquitetura atual.
