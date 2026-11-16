# SPEC: Fire TV Kiosk Controller

> Documento de especificação (Spec-Driven Development).
> Regra de idioma: **todos os identificadores internos (variáveis, funções, classes, chaves de config, nomes de arquivos, campos de JSON) em inglês.** Mensagens exibidas ao usuário final podem estar em português.

## 1. Objetivo

Controlar, a partir de um computador na mesma rede, um Fire TV Stick com **Fire OS (Android)** para que ele funcione como um terminal que **abre direto numa página web em tela cheia** e **se recupera sozinho** de travamentos, sem depender do controle remoto.

## 2. Contexto e premissas

- O Fire Stick é acessado por **ADB via Wi-Fi**.
- Modelos com **Vega OS** (Fire Stick HD 2026, 4K Select, 4K 3ª geração) **não suportam** este projeto. O script detecta a falha de conexão ADB e explica isso.
- A Amazon bloqueou conexões ADB **locais** em atualizações recentes do Fire OS; o controlador roda **no PC / Raspberry Pi**, nunca no aparelho.
- Navegador kiosk: **Fully Kiosk Browser** (`de.ozerov.fully`). O APK é fornecido pelo usuário; o projeto **não baixa nada**.
- A tela inicial da Amazon não pode ser removida; o projeto a contorna, mas **não a elimina**.

## 3. Escopo

Dentro: conectar e validar compatibilidade; instalar o APK; abrir o kiosk (ou Silk via VIEW) na URL; modo `watch`; reboot e limpeza de cache em horários; desativar/restaurar apps da Amazon de forma reversível; logs e indicador de saúde.

Fora: Vega OS; contornar Home permanentemente; baixar APKs, gerenciar contas/credenciais; interface gráfica.

## 4. Estrutura

```
firetv-kiosk/
├── CLAUDE.md  SPEC.md  README.md  config.example.json
├── src/firetv_kiosk.py            # script principal, com --self-test embutido
├── scripts/validate.py            # validação externa: sintaxe + self-test + status
├── scripts/hook_post_edit.py      # hook PostToolUse (Edit/Write/MultiEdit/Bash)
├── scripts/hook_stop.py           # hook Stop
├── scripts/watch_files.py         # observador de arquivos para alterações externas
├── .claude/settings.json          # hooks
├── .claude/validation-status.json # gerado, ignorado no git
└── logs/                          # gerado, ignorado no git
```

Python 3.10+, apenas biblioteca padrão.

## 5. Configuração (`config.json`)

| Key | Type | Default | Description |
|---|---|---|---|
| `device_ip` | string | (required) | IP do Fire Stick |
| `adb_port` | int | `5555` | Porta ADB |
| `adb_binary` | string | `"adb"` | Caminho do executável adb (auto-detecção se no PATH/pastas comuns) |
| `kiosk_package` | string | `"de.ozerov.fully"` | Package do navegador kiosk |
| `kiosk_apk_path` | string | `null` | Caminho local do APK |
| `start_url` | string | (required) | URL a abrir (http/https) |
| `launch_mode` | string | `"kiosk_app"` | `"kiosk_app"`, `"kiosk_app_url"` ou `"silk_intent"` |
| `watch_interval_seconds` | int | `60` | Intervalo do modo watch |
| `max_relaunch_attempts` | int | `3` | Tentativas antes de reiniciar o aparelho |
| `reboot_times` | list[string] | `[]` | Horários `"HH:MM"` de reinicialização diária |
| `disabled_packages` | list[string] | `[]` | Packages da Amazon a desativar |
| `log_file` | string | `"logs/firetv_kiosk.log"` | Arquivo de log |
| `state_file` | string | `"logs/firetv_kiosk_state.json"` | Estado dos pacotes desativados (FR-09) |

Validações: IP válido, porta 1–65535, URL http/https, `HH:MM`, intervalos positivos, chaves desconhecidas rejeitadas. Config inválida interrompe com erro claro e código ≠ 0.

## 6. CLI

`python src/firetv_kiosk.py --config config.json <command>`

| Command | Behavior | Exit |
|---|---|---|
| `check` | Conecta, confirma Fire OS e kiosk instalado. Não altera nada. | 0/1 |
| `setup` | `check` + instala APK se ausente + aplica `disabled_packages`. | 0/1 |
| `launch` | Abre a `start_url` conforme `launch_mode`. | 0/1 |
| `watch` | Loop de recuperação (FR-06..08, reboot_times). `--once` faz uma verificação. | sinal |
| `reboot` | Reinicia o aparelho. | 0/1 |
| `clear-cache` | `pm clear` do kiosk somente com `--yes`. | 0/1 |
| `restore` | Reativa os pacotes registrados no estado. | 0/1 |
| `discover` | Procura aparelhos com ADB aberto na sub-rede local (não requer config). | 0/1 |
| `open` | Um clique: sem `config.json`, acha o Fire TV e grava o config; conecta e abre a `start_url`. Sem o kiosk instalado, cai para `silk_intent`. Usado por `open_kiosk.bat` / `open_kiosk.command`. | 0/1 |
| `--self-test` | Testes embutidos; não executa operação normal. | 0/1/2 |

Flag global `--dry-run`: imprime os comandos ADB sem executá-los.

## 7. Requisitos funcionais

| ID | Requirement |
|---|---|
| FR-01 | `check` falha com mensagem sobre possível Vega OS após 3 tentativas de conexão. |
| FR-02 | `setup` idempotente. |
| FR-03 | `setup` não instala APK inexistente ou vazio. |
| FR-04 | `kiosk_app`: `monkey -p <package> -c android.intent.category.LAUNCHER 1`. |
| FR-05 | `silk_intent`: `am start -a android.intent.action.VIEW -d <url>` com URL escapada. |
| FR-06 | `watch` detecta primeiro plano via `dumpsys window` e reabre o kiosk. |
| FR-07 | `watch` reconecta com backoff. |
| FR-08 | Após `max_relaunch_attempts` falhas seguidas, reinicia e zera o contador. |
| FR-09 | Pacotes desativados registrados em arquivo de estado para `restore`. |
| FR-10 | Nenhum comando destrutivo sem `--yes`. |
| FR-11 | Todo comando ADB registrado com timestamp, código de retorno e duração. |
| FR-12 | Falha em etapa obrigatória interrompe as dependentes. |

## 8. Não funcionais

Sem credenciais; `config.json` no `.gitignore`. Timeout em todo subprocesso ADB; timeout nunca é sucesso. Toda chamada ADB via `AdbRunner`. Logs com rotação; indicador de saúde.

## 9–10. Salvaguardas e indicador de saúde

Ver `CLAUDE.md` (regras vigentes e operacionais).

## 11. Plano de testes

Config, Connection, Setup, Launch, Watch, Packages, Safety, Discover, Self-test (em `src/firetv_kiosk.py`) e Validation/Hooks/Watcher (em `scripts/validate.py`).

## 12. Critérios de aceitação

1. `python src/firetv_kiosk.py --self-test` → APROVADO, código 0.
2. Com aparelho real Fire OS: `check`, `setup`, `launch` e `watch` conforme a seção 6.
3. Matar o kiosk faz o `watch` reabri-lo em até `watch_interval_seconds`.
4. Demonstração das salvaguardas em cópia temporária.
5. `CLAUDE.md` contém as regras das seções 9 e 10.

## 14. Decisões (questões em aberto resolvidas)

- **Arquitetura híbrida:** o computador é usado uma vez para `setup`/`launch`; depois o próprio Fully mantém a página (Launch on Boot, Bring to Foreground, Auto Reload). `watch` é reforço opcional.
- **Host:** desconhecido → código multiplataforma com detecção do `adb` e comando `discover` para achar o IP.
- **URL:** `https://piltover.grupoinlog.com.br/tv`. Configuração do Fully feita manualmente uma vez (sem API remota, sem senha armazenada). `launch_mode: kiosk_app_url` tenta abrir o Fully já na URL e cai para o LAUNCHER se o intent falhar.
- **disabled_packages:** vazio; sugestões no README.
