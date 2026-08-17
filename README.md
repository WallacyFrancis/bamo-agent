# bamo-agent

Assistente pessoal local cujo motor é o `agy` (Antigravity). O Bamo media a
própria conversa, mantém memória de longo prazo aprendida automaticamente,
sessões de curto prazo e conhecimento consolidado (OKF) — tudo em arquivos
locais, sem banco de dados nem serviços externos.

## Requisitos

- Python 3.11+
- `agy` disponível no `PATH`
- `cryptography` — só para o cofre de segredos (`vault`/`secret`); o resto
  do Bamo é stdlib puro. `keyring` é opcional: se instalado e funcional, o
  cofre o usa para proteger a chave de dados; senão, cai automaticamente
  para senha mestra (Argon2id).

## Uso

```bash
cd /home/wallacy/Documentos/bamo-agent
python3 bamo.py chat
python3 bamo.py ask "Liste os próximos passos deste projeto"
```

`chat` abre uma sessão de conversa: o Bamo lê cada mensagem, monta o contexto
(regras + memória-base + resumo da sessão + memórias relevantes), chama o
`agy` para responder e grava o turno. Digite `sair` (ou `/sair`, `exit`,
`quit`, ou Ctrl+D) para encerrar. `ask` faz o mesmo fluxo para uma pergunta
única, como uma conversa de um turno só.

### Memória, sessões e conhecimento

```bash
python3 bamo.py memory list [--status active|superseded|blocked|forgotten]
python3 bamo.py memory show <id>
python3 bamo.py memory search "<consulta>"
python3 bamo.py memory correct <id> [--content "..."]
python3 bamo.py memory forget <id> --confirm <id>
python3 bamo.py memory block <id>
python3 bamo.py memory learning on|off

python3 bamo.py session list
python3 bamo.py session show <id>
python3 bamo.py session delete <id> --confirm <id>

python3 bamo.py knowledge list
python3 bamo.py knowledge show <id>
python3 bamo.py knowledge forget <id> --confirm <id>
```

### Cofre local de segredos

```bash
python3 bamo.py vault init [--key-provider system|password]
python3 bamo.py vault status
python3 bamo.py vault lock
python3 bamo.py vault rotate-key --confirm <vault-id>

python3 bamo.py secret set <label> [--stdin]
python3 bamo.py secret list
python3 bamo.py secret delete <entry-id> --confirm <entry-id>
python3 bamo.py secret audit
```

O cofre (`secrets/vault.enc`) guarda credenciais informadas explicitamente
pelo usuário, criptografadas em repouso com Fernet (`cryptography`). A chave
de dados é protegida pelo keyring do sistema quando disponível; sem keyring
funcional, cai automaticamente para senha mestra derivada com Argon2id —
pedida sem eco a cada comando de cofre, nunca cacheada. `secret set` lê o
valor por entrada oculta (ou `--stdin` para automação; o valor nunca vai por
argumento de linha de comando). Não há `secret show`/`get`/exportação nesta
fase: o cofre nunca revela um valor em tela nem repassa segredos para
`chat`/`ask`, para a memória ou para o `agy` — é um módulo isolado do resto
do Bamo. `secret delete` e `vault rotate-key` exigem `--confirm` idêntico ao
alvo, como os demais comandos destrutivos.

### Conectores e agendamento local

```bash
python3 bamo.py connector list
python3 bamo.py connector show <connector-id>
python3 bamo.py connector create-demo <nome>
python3 bamo.py connector enable <connector-id> --confirm <connector-id>
python3 bamo.py connector disable <connector-id> --confirm <connector-id>
python3 bamo.py connector delete <connector-id> --confirm <connector-id>

python3 bamo.py connector run <connector-id> <capability> --confirm <connector-id>
python3 bamo.py connector audit [--connector <connector-id>]

python3 bamo.py schedule set <connector-id> <capability> --every <minutos> --confirm <connector-id>
python3 bamo.py schedule list
python3 bamo.py schedule disable <schedule-id> --confirm <schedule-id>

python3 bamo.py scheduler run
```

Um conector (`connectors/<id>.json`) declara um `provider` e uma lista
fechada de `capabilities`, ambos validados contra uma allowlist implementada
em código (`core/connectors.py`) — nunca texto livre do usuário. `agy`,
`chat` e `ask` nunca criam, alteram nem executam conector algum; só a CLI
explícita faz isso. `create-demo` cria o único provider desta fase,
`local-demo`, que opera exclusivamente sobre dados sintéticos gerados em
memória — sem rede, sem navegador, sem credencial real.

`connector run` mostra o conector, a capacidade e seu efeito antes de pedir
confirmação; a confirmação é o ID exato do conector e vale só para aquela
execução. O dispatcher (`core/dispatcher.py`) inicia o executor do provider
como processo filho isolado (`subprocess`, sem shell, ambiente mínimo,
diretório de trabalho controlado, timeout e tamanho máximo de saída
obrigatórios); o `agy` nunca vê esse processo nem seu resultado bruto. Toda
execução — manual ou por agenda — gera um evento em `connector-audit/`,
redigido e sem payload bruto, headers, cookies ou segredo.

`schedule set` só aceita capacidades `read`/`notify` já habilitadas no
conector, recusa intervalos abaixo do mínimo aprovado e agendas duplicadas.
`bamo scheduler run` processa as agendas devidas numa única execução, sob
lock que impede sobreposição — pensado para ser chamado pelo cron do
sistema (ex.: `*/5 * * * * cd /caminho/do/projeto && python3 bamo.py
scheduler run`); o Bamo nunca instala nem edita crontab sozinho. Nenhuma
capacidade deste PRD usa credencial do cofre — `secret_id` existe no schema
do conector como referência reservada para um provider futuro.

Memória de longo prazo e OKFs são criados **automaticamente** ao longo da
conversa (sem confirmar item a item) sempre que houver evidência clara e a
informação for durável — nunca para dados sensíveis (saúde, biometria,
religião, política, sexualidade, finanças, jurídico) nem para segredos, que
são sempre substituídos por `[REDACTED]` antes de qualquer gravação.
`memory learning off` desliga esse aprendizado automático sem afetar o
registro normal da conversa; `bamo memory forget`/`session delete`/
`knowledge forget` exigem `--confirm <id>` idêntico ao alvo.

## Limites

- O motor é somente `agy` nesta fase; nenhuma outra API de LLM é usada.
- O Bamo nunca cria, altera ou remove arquivos em `skills/`, o próprio
  código (`bamo.py`, `core/`) ou configuração do runtime `agy` sem
  solicitação explícita do usuário.
- Sessões (memória de trabalho) são retidas por 30 dias; a limpeza de
  sessões expiradas roda ao usar `chat`, `ask` ou `session list` e nunca
  apaga memórias de longo prazo já consolidadas.
- A redação de segredos em conversa/memória/OKF continua best-effort (regex
  local); o cofre é o único lugar com garantia criptográfica, e só guarda o
  que o usuário grava explicitamente com `secret set` — nunca o que é dito
  em `chat`/`ask`.
- O cofre não injeta segredos em nada (agy, shell, variáveis de ambiente,
  clipboard) — o PRD-004 entrega a infraestrutura de conectores, mas nenhuma
  capacidade real de serviço externo (ex.: LinkedIn, Instagram) usa
  credencial nesta fase; isso é escopo de PRDs futuros por provider.

## Estrutura de dados

```text
bamo-agent/
├── bamo.py            # CLI
├── core/               # sessão, memória, conhecimento, integração com agy
├── memory/
│   ├── CORE.md          # memória-base, versionada
│   └── facts/            # memórias de longo prazo (gerado, não versionado)
├── knowledge/
│   ├── README.md          # definição do formato OKF, versionada
│   └── okf/                # OKFs gerados (gerado, não versionado)
├── sessions/                # sessões de conversa (gerado, não versionado)
├── secrets/
│   └── vault.enc              # cofre cifrado (gerado, não versionado, 0600)
├── connectors/
│   └── conn-*.json            # registro de conector (gerado, não versionado, 0600)
├── schedules/
│   └── sched-*.json           # agenda local de conector (gerado, não versionado, 0600)
├── connector-audit/
│   └── YYYY-MM.jsonl          # eventos de execução redigidos (gerado, não versionado, 0600)
├── executors/
│   └── local_demo.py           # executor isolado do provider local-demo (versionado)
└── settings.local.json       # {"learning_enabled": true|false} (gerado)
```

Configurações, segredos e credenciais não devem ser versionados.

## Licença

MIT — veja [LICENSE](LICENSE). Repositório oficial: a ser publicado em `github.com/WallacyFrancis/bamo-agent`.
