# CLAUDE.md — firetv-kiosk

Controlador (Python 3.10+, só biblioteca padrão) que mantém um Fire TV Stick (Fire OS) aberto em
`https://piltover.grupoinlog.com.br/tv` via Fully Kiosk Browser. Roda no PC, nunca no aparelho.
Especificação completa: `SPEC.md`. Uso: `README.md`.

## Regra de idioma
Todos os identificadores internos (variáveis, funções, classes, chaves de config/JSON, nomes de
arquivos) em **inglês**. Mensagens ao usuário final podem estar em português.

## Arquitetura
- `src/firetv_kiosk.py`: CLI + lógica + suíte embutida (`--self-test`). Toda chamada ADB passa por
  `AdbRunner` (`SubprocessAdbRunner`, `DryRunAdbRunner`, `FakeAdbRunner` para testes).
- `scripts/validate.py`: validação externa (sintaxe + todas as suítes + indicador de saúde).
- `scripts/hook_post_edit.py`, `scripts/hook_stop.py`: hooks do Claude Code (`.claude/settings.json`).
- `scripts/watch_files.py`: observador para alterações feitas fora do Claude.

## Salvaguardas automáticas (obrigatório)

### 1. Testes obrigatórios
Todo novo comando, função ou comportamento exige testes cobrindo **sucesso, entrada inválida e erro
relevante**, verificando **comportamento observável** (comandos ADB emitidos, código de saída,
mensagens, estado gravado) — não apenas "não lançou exceção". Testes do script principal ficam nas
classes `*Tests` de `src/firetv_kiosk.py` e devem ser registrados em `TEST_CASES`; testes das
salvaguardas ficam em `scripts/validate.py` (`SELF_TEST_CASES`).

### 2. Autoteste embutido
- `python src/firetv_kiosk.py --self-test` roda a suíte e sai **sem** executar operação normal.
- Usa `FakeAdbRunner` e `tempfile`; nunca toca o aparelho real nem o `config.json` real.
- A execução normal valida entradas, pré-condições e resultados; etapa obrigatória que falha
  interrompe as dependentes (FR-12) com erro claro e código ≠ 0.

### 3. Execução automática após alterações
- `PostToolUse` (`Edit|Write|MultiEdit|NotebookEdit`): valida quando o arquivo editado é monitorado
  (`src/**/*.py`, `scripts/**/*.py`, `config.example.json`, `.claude/settings.json`).
- `PostToolUse` (`Bash`): valida quando o hash do conteúdo monitorado difere do último validado
  (alterações feitas pelo terminal).
- `Stop`: impede encerrar com status REPROVADO / NÃO VALIDADO para o conteúdo atual; respeita
  `stop_hook_active` (não bloqueia de novo → sem ciclo infinito).
- `python scripts/watch_files.py`: cobre alterações feitas fora do Claude (precisa estar rodando).
- A verificação de sintaxe é **externa** (`compile()` em `validate.py`), pois um script com sintaxe
  inválida não consegue iniciar seus próprios testes.
- Arquivos gerados (`.claude/validation-status.json`, `logs/`, `__pycache__`) **não** são monitorados,
  então relatórios não disparam nova validação.

### 4. Preservação dos testes
Proibido remover, ignorar (`skip`) ou enfraquecer testes para obter aprovação. Teste ignorado
resulta em NÃO VALIDADO. Se um requisito mudar legitimamente, explicar a mudança e atualizar os
testes preservando a cobertura dos comportamentos ainda exigidos.

### 5. Alertas de falha
Cada falha informa: script e comportamento afetados, nome do teste, esperado × observado,
local (`arquivo:linha`) e comando de reprodução (ex.: `python src/firetv_kiosk.py --self-test --test <nome>`).
Os executores retornam código ≠ 0 em falha; o hook converte em exit 2 + stderr (chega ao Claude).

### 6. Correção e comprovação
Ao detectar falha: corrigir e **reexecutar toda a bateria** (`python scripts/validate.py`).
Não declarar a tarefa concluída com validação pendente.

## Indicador de saúde
Após cada validação é exibido e gravado em `.claude/validation-status.json`
(`status, total_tests, failed_tests, skipped_tests, timestamp, content_hash, failures`):

| Status | Condição | Exit |
|---|---|---|
| **APROVADO** | Todos os testes obrigatórios e a verificação de sintaxe passaram | 0 |
| **REPROVADO** | Falha de teste ou de sintaxe | 1 |
| **NÃO VALIDADO** | Testes não executaram, incompletos, ignorados, timeout, suíte ausente ou código de saída incoerente | 2 |

Qualquer alteração em arquivo monitorado muda o `content_hash` e invalida o resultado anterior
(`python scripts/validate.py --status` mostra se está desatualizado). Ausência de testes, timeout ou
dependência indisponível **nunca** equivalem a APROVADO. APROVADO significa que os comportamentos
cobertos passaram — não garante ausência de qualquer erro.

## Comandos úteis
```bash
python scripts/validate.py            # bateria completa + indicador
python scripts/validate.py --status   # último resultado / se está desatualizado
python src/firetv_kiosk.py --self-test
python scripts/validate.py --self-test
python scripts/watch_files.py         # observador externo
```
