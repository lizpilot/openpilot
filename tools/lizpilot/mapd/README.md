# mapd no LizPilot: como o binário chega ao comma

O mapd (github.com/pfeiferj/mapd, licença MIT) lê o OpenStreetMap sem internet e publica o limite da via (`mapdOut`). O LizPilot **não guarda o binário no repositório**. Quando o dono toca em "Instalar o mapd" no celular, o comma baixa a versão fixada em `openpilot/selfdrive/hexa/speed_limit.py` (`MAPD_URL`, `MAPD_SHA256`, `MAPD_VERSION`).

As estruturas em `openpilot/cereal/custom.capnp` foram copiadas do mapd no commit `8e5eee6` (21/09/2026). O binário precisa ser **desse mesmo schema**.

## O binário: o `8e5eee6` com 25 leitores no msgq (MAP-02)

Não há release do autor nesse commit (a última release é a v2.3.1, de outro schema). Então compilamos nós, num computador confiável.

**Por que o patch:**
- O `8e5eee6` usa o `gomsgq` v0.1.11, com 15 leitores fixos por fila.
- O msgq do canal (`a95f3c1`) tem 25 (commaai/msgq `d44b74a`, 06/09/2026).
- Com números diferentes, o cabeçalho da fila fica 240 bytes fora do lugar, e o `loggerd` nunca lê um `mapdOut` (MAP-01).
- `0001-msgq-25-reader-slots.patch` (nesta pasta) passa ao `gomsgq` v0.1.12 e abre as filas com 25. A variável `MSGQ_MAX_READERS=15` serviria a um msgq antigo.
- O schema não muda.

**Ferramentas,** baixadas dos sites oficiais com o sha256 conferido:
- Go `go1.27.3` (darwin-arm64);
- Zig `0.17.1`;
- zlib `1.4`, compilada estática para `aarch64-linux-musl` (sha256 `02dd159783677a749c04adbdd02b55b8a2bcc7e054f51b76b549199a916ad584`) e entregue ao cgo por um `pkg-config` mínimo que só responde pela zlib.

```bash
git clone https://github.com/pfeiferj/mapd && cd mapd && git checkout 8e5eee69f7904ed98eed91ceff4ab5919bec0e3c
git am <openpilot>/tools/lizpilot/mapd/0001-msgq-25-reader-slots.patch   # sha256 d7aeeb2c…0eee3
export GOTOOLCHAIN=local GOFLAGS=-mod=readonly   # PATH com o go, o zig e o pkg-config mínimo
go mod verify
CGO_ENABLED=1 GOOS=linux GOARCH=arm64 CC="zig cc -target aarch64-linux-musl" \
  go build -trimpath -buildvcs=false -tags netgo,osusergo \
  -ldflags='-s -w -buildid= -linkmode=external -extldflags "-static -s"' -o mapd .
file mapd             # ELF 64-bit LSB executable, ARM aarch64, statically linked, stripped
shasum -a 256 mapd    # 7305d508e67e738c17b9a9dbbcb5a16076026615f6c233bb1340675ac88abb8b, 20723280 bytes
```

**Reprodutível:** duas compilações com os caches do Go e do Zig vazios deram o mesmo hash. A mesma receita sem o patch dá o `b8b6c8a2…` da `mapd-8e5eee6`.

Publique como anexo de uma Release do `CPontes/openpilot` (`mapd-8e5eee6-msgq25`), **só com o OK do dono**. Os pinos em `speed_limit.py`:

```python
MAPD_VERSION = "8e5eee6-msgq25"
MAPD_URL = "https://github.com/CPontes/openpilot/releases/download/mapd-8e5eee6-msgq25/mapd"
MAPD_SHA256 = "7305d508e67e738c17b9a9dbbcb5a16076026615f6c233bb1340675ac88abb8b"
```

**Para voltar:** os pinos da `mapd-8e5eee6` (`b8b6c8a2…`), ou desligar o limite no celular. A troca vale na próxima partida.

Com pinos novos, o binário antigo no comma deixa de bater o hash e não roda. O dono toca de novo em "Instalar o mapd" no celular, com o carro desligado.

Enquanto os dois estiverem vazios, o comma não baixa nada e o mapd não roda. `test_the_shipped_pins_are_empty_or_complete` impede deixar só um preenchido.

## O que o comma confere

- **Na instalação:** só `https://`, até 64 MB, sha256 igual ao fixado; grava com `fsync` antes de virar executável.
- **Sempre que o manager decide se o mapd roda:** o arquivo em `/data/lizpilot/mapd/mapd` precisa ter o sha256 fixado (`binary_ok`, cache por tamanho e mtime). Um binário trocado depois da instalação não roda.
- **As mensagens do mapd** são lidas dos bytes crus (`speed_limit.decode`): outra união ou bytes inválidos viram "sem limite", nunca uma exceção. Nem o `selfdrived` nem o painel põem `mapdOut` no SubMaster.

## Como roda

- `nice -n 10`, `GOMAXPROCS=2`; não é processo de direção (`NON_DRIVING_PROCESSES`): se cair, o carro não desengata.
- O aviso de limite tem prioridade `LOWEST`: abaixo do aviso de radar.

## Logs

- `mapdOut` (nome da rua e limite): só no rlog, nunca no qlog.
- `mapdExtendedOut` (posição e caminho à frente) e `mapdIn`: não gravados.

## Mapas

O comma lê `/data/openpilot/mapd_download_menu.json`, que está na raiz do repositório. Ele oferece:
- `br_state.RJ`: o estado do Rio, em 6 arquivos da grade de 2°;
- `nation.BR`: o Brasil inteiro.

O download parte do celular e só começa com o **carro desligado**, num **Wi-Fi** (rede não medida) e com **espaço livre** (2 GB para o Rio, 12 GB para o Brasil: valores de folga, os tamanhos reais não foram medidos [hipótese]). Se o carro ligar no meio, o painel manda `cancelDownload` uma vez.
