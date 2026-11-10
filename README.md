# Fire TV Kiosk — piltover

Faz um Fire TV Stick (Fire OS) abrir sozinho em **https://piltover.grupoinlog.com.br/tv**, em tela cheia,
e voltar para essa página se algo der errado.

## Como funciona (resumo)

1. **Uma vez**, de um computador na mesma rede Wi-Fi do Fire Stick, você roda o `setup`: ele instala o
   Fully Kiosk Browser no aparelho pela rede (ADB) e abre a página.
2. Você configura o Fully **uma vez** pelo controle remoto (5 minutos, passo a passo abaixo).
3. Depois disso **o Fire Stick funciona sozinho**: o Fully abre ao ligar a TV, volta para a frente se
   alguém sair, e recarrega a página se ela der erro. O computador pode ser desligado.
4. Opcional: deixar um computador/Raspberry Pi rodando `watch` como reforço (reabre o kiosk,
   reinicia o aparelho se travar e reinicia todo dia às 04:00).

> Por que precisa de computador? A Amazon bloqueou o ADB "local" dentro do próprio Fire Stick, então a
> instalação/controle precisa vir de outro aparelho na rede.

> **Modelos NÃO suportados:** Fire TV Stick HD (2026), 4K Select e 4K (3ª geração) usam **Vega OS**, que
> não permite instalar APK nem ADB. Se o `check` não conectar de jeito nenhum, provavelmente é isso.

---

## Passo 1 — Preparar o Fire Stick (controle remoto)

1. **Configurações > Minha Fire TV > Sobre**: clique 7 vezes em *Fire TV Stick* até aparecer
   "Você agora é um desenvolvedor".
2. **Configurações > Minha Fire TV > Opções do desenvolvedor**:
   - **Depuração ADB** → ATIVADO
   - **Apps de fontes desconhecidas** → ATIVADO (se aparecer)
3. Anote o IP em **Configurações > Minha Fire TV > Sobre > Rede** (ex.: `192.168.0.50`).
   Não achou? O comando `discover` (passo 3) procura para você.

## Passo 2 — Preparar o computador

Funciona em Windows, Mac ou Linux.

1. Instale o **Python 3.10+** (https://www.python.org/downloads/ — no Windows marque "Add Python to PATH").
2. Instale o **Android SDK Platform-Tools** (https://developer.android.com/tools/releases/platform-tools),
   extraia a pasta e coloque-a no PATH (ou informe o caminho completo do `adb` em `adb_binary`).
   O script também procura o `adb` nos lugares mais comuns automaticamente.
3. Baixe o APK do **Fully Kiosk Browser** no site oficial (fully-kiosk.com) e salve em `apk/fully-kiosk.apk`
   dentro desta pasta. O projeto não baixa nada sozinho.

## Passo 3 — Descobrir o IP e configurar

```bash
python src/firetv_kiosk.py discover
```

Na primeira conexão a TV mostra **"Permitir depuração USB?"** — marque *Sempre permitir* e confirme.
Depois:

```bash
cp config.example.json config.json
```

(no Windows: `copy config.example.json config.json`) e troque o `device_ip` pelo IP encontrado.

## Passo 4 — Instalar e abrir

```bash
python src/firetv_kiosk.py --config config.json check
```

```bash
python src/firetv_kiosk.py --config config.json setup
```

```bash
python src/firetv_kiosk.py --config config.json launch
```

Para só ver o que seria feito, sem mexer no aparelho, acrescente `--dry-run`.

## Passo 5 — Configurar o Fully (uma vez, no controle remoto)

Com o Fully aberto, abra o menu de configurações (deslize da borda esquerda / botão de menu do controle) e ajuste:

| Onde (Settings) | Opção | Valor |
|---|---|---|
| Web Content Settings | Start URL | `https://piltover.grupoinlog.com.br/tv` |
| Device Management | Launch on Boot | ativado |
| Device Management / Kiosk | Bring to Foreground (após sair do app) | ativado |
| Web Auto Reload | Auto Reload on Page Error / Network Reconnect | ativado |
| Device Management | Keep Screen On | ativado |

Os nomes podem variar um pouco conforme a versão do Fully. Algumas opções de kiosk exigem a licença
Fully **Plus**. Não armazenamos senha do Fully neste projeto.

Pronto: desligue e ligue a TV — o Fully deve abrir direto no piltover.

## Opcional — reforço com `watch`

```bash
python src/firetv_kiosk.py --config config.json watch
```

Verifica a cada `watch_interval_seconds` se o kiosk está na frente; reabre se não estiver; após
`max_relaunch_attempts` falhas seguidas reinicia o Fire Stick; reconecta sozinho se o ADB cair; e nos
horários de `reboot_times` limpa caches (sem apagar dados) e reinicia.

Para rodar sempre:
- **Linux / Raspberry Pi** (systemd): crie `/etc/systemd/system/firetv-kiosk.service` com
  `ExecStart=/usr/bin/python3 /caminho/firetv-kiosk/src/firetv_kiosk.py --config /caminho/firetv-kiosk/config.json watch`,
  `Restart=always`, e rode `sudo systemctl enable --now firetv-kiosk`.
- **Windows**: Agendador de Tarefas → "Ao fazer logon" → `python C:\caminho\src\firetv_kiosk.py --config C:\caminho\config.json watch`.
- **Mac**: um LaunchAgent (`~/Library/LaunchAgents`) com o mesmo comando e `KeepAlive`.

## Comandos

| Comando | O que faz |
|---|---|
| `discover [--subnet 192.168.0.0/24]` | Procura aparelhos com ADB na rede |
| `check` | Conecta, confere Fire OS e kiosk instalado (não altera nada) |
| `setup` | `check` + instala o APK se faltar + desativa `disabled_packages` (idempotente) |
| `launch` | Abre a URL conforme `launch_mode` |
| `watch [--once]` | Vigia e recupera (veja acima) |
| `reboot` | Reinicia o Fire Stick |
| `clear-cache --yes` | `pm clear` do kiosk — **apaga todos os dados e configurações do Fully** |
| `restore` | Reativa os apps desativados pelo `setup` |

`launch_mode`: `kiosk_app_url` (padrão no exemplo: abre o Fully já na URL; se o Fully não aceitar,
cai para abrir o app normalmente), `kiosk_app` (abre o Fully, que usa sua Start URL) ou `silk_intent`
(usa o navegador Silk da Amazon, sem kiosk).

## Desativar apps da Amazon (opcional, reversível)

`disabled_packages` começa vazio. Candidatos comuns (teste um por vez; nunca desative o launcher
`com.amazon.tv.launcher`):

- `com.amazon.venezia` (Appstore — atenção: impede atualizar apps pela loja)
- `com.amazon.bueller.photos` (Fotos)
- `com.amazon.tv.livetv` (TV ao vivo)
- `com.amazon.avod.thirdpartyclient` (Prime Video)

Tudo que for desativado fica registrado em `logs/firetv_kiosk_state.json`; `restore` reativa.

## Limitações

- A tela inicial da Amazon não pode ser removida; o botão Home do controle ainda sai do kiosk
  (o Fully/`watch` traz de volta, mas não impede).
- Vega OS não é suportado.
- Testado com ADB simulado; versões diferentes do Fire OS podem se comportar diferente.

## Desenvolvimento

Veja `CLAUDE.md` (salvaguardas automáticas) e `SPEC.md`.

```bash
python scripts/validate.py
```
