# bamo-agent

MVP local de um assistente extensível cujo motor inicial é o `agy` (Antigravity).
O Bamo preserva memória e skills no próprio diretório, mas não altera skills por conta própria.

## Requisitos

- Python 3.11+
- `agy` disponível no `PATH`

## Uso

```bash
cd /home/wallacy/Documentos/bamo-agent
python3 bamo.py chat
python3 bamo.py ask "Liste os próximos passos deste projeto"
```

`chat` abre uma sessão interativa do `agy` já instruída com a memória-base e as skills instaladas. `ask` executa uma pergunta única.

## Limites do MVP

- O motor é somente `agy` nesta fase.
- O Bamo nunca cria, altera ou remove arquivos em `skills/` sem solicitação explícita do usuário.
- Memórias e artefatos de conhecimento pertencem a `memory/` e `knowledge/`; a política de atualização será aprovada por PRD antes de qualquer escrita automática.
- Configurações, segredos e credenciais não devem ser versionados.

## Licença

MIT — veja [LICENSE](LICENSE). Repositório oficial: a ser publicado em `github.com/WallacyFrancis/bamo-agent`.
