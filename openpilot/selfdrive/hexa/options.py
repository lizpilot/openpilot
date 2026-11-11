"""Hexa options: the single list of the fork's own settings.

The comma screen (selfdrive/ui/mici/layouts/settings/hexa.py) and the phone panel (system/hexaweb) are both
built from this list, so a new option only has to be added here. New options are off by default, except
fixes the car needs, which stay switchable so they can be turned off without an update.
"""
from dataclasses import dataclass, field

from openpilot.selfdrive.hexa import edition

# (key, title) in display order
GROUPS: tuple[tuple[str, str], ...] = (
  ("direcao", "Direção"),
  ("velocidade", "Velocidade"),
  ("avisos", "Avisos"),
  ("celular", "Celular"),
)
GROUP_KEYS = tuple(k for k, _ in GROUPS)

# The comma's own longitudinal (AlphaLongitudinalEnabled) was never tested nor calibrated in this car. The phone
# says so when a speed bump asks for less than the Haval ACC goes. True only after the calibration (LONG-02).
LONG_CALIBRATED = False


@dataclass(frozen=True)
class HexaOption:
  key: str  # Params key
  group: str
  title: str  # short, lowercase like the rest of the comma 4 UI
  description: str
  kind: str = "bool"  # "bool" or "enum" (enum is stored as the choice index, an INT param)
  choices: tuple[str, ...] = field(default_factory=tuple)
  default: bool | int = False
  # Changes how the car drives: the phone may only change it with the car parked or off, and it is
  # applied on the next engagement (or the next start, if restart_needed).
  driving: bool = True
  restart_needed: bool = False
  # Some options can only be changed on the comma itself (e.g. turning the phone panel on).
  phone_writable: bool = True
  # Why the option can't be offered yet. It stays in the list so it isn't forgotten, hidden everywhere.
  blocked: str = ""
  # Still being proven in the car: the comma and the phone show an "em teste" tag next to it.
  testing: bool = False
  # Only offered in these car brands (edition.py): what was measured and tuned on the Haval alone stays off elsewhere.
  brands: tuple[str, ...] = ()

  def coerce(self, value):
    """Validate a value coming from outside (the phone). Returns the value to store or raises ValueError."""
    if self.kind == "bool":
      if isinstance(value, bool):
        return value
      raise ValueError(f"{self.key}: expected true/false")
    if self.kind == "enum":
      if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < len(self.choices):
        raise ValueError(f"{self.key}: expected 0..{len(self.choices) - 1}")
      return value
    raise ValueError(f"{self.key}: unknown kind {self.kind}")


OPTIONS: tuple[HexaOption, ...] = (
  HexaOption(
    key="LowSpeedFilter",
    brands=edition.GWM_ONLY,
    group="direcao",
    title="volante calmo em baixa velocidade",
    description="Abaixo de 30 km/h, o volante ignora as tremidas pequenas do modelo e fica reto.\n" +
                "Curvas de verdade continuam passando, com um pequeno atraso.",
    default=True,
    restart_needed=True,
  ),
  HexaOption(
    key="SoftHandoff",
    brands=edition.GWM_ONLY,
    group="direcao",
    title="devolver o volante aos poucos",
    description="Quando você força o volante de leve contra o openpilot, ele solta a força aos poucos, em menos de 1 s, e volta de onde parou " +
                "ao você aliviar. Na curva, o volante não volta de repente. Com força firme (acima de 1,5 Nm), solta na hora, como hoje.\n" +
                "Em teste: o panda não muda.",
    restart_needed=True,
    testing=True,
  ),
  HexaOption(
    key="SteerReversalEase",
    brands=edition.GWM_ONLY,
    group="direcao",
    title="soltar o volante aos poucos na troca de lado",
    description="Quando o openpilot muda de ideia e passa a puxar o volante para o outro lado enquanto você ainda segura para o lado " +
                "de antes, ele não solta a força de uma vez: ela desce no ritmo normal até zero, e só então ele solta. " +
                "Isso tira o tranco curto nas viradas. Se você força contra o que ele pede e contra a força que ele está fazendo, " +
                "solta na hora, como hoje.\n" +
                "Em teste: o panda não muda.",
    restart_needed=True,
    testing=True,
  ),
  HexaOption(
    key="AlwaysOnLateral",
    brands=edition.GWM_ONLY,
    group="direcao",
    title="direção sempre ativa",
    description="O freio pausa só o acelerador e o freio do ACC; a direção continua.\n" +
                "Em teste: depois de frear, o volante segue ativo. Puxe a alavanca para voltar o ACC. Só o cancelar desliga a direção.",
    restart_needed=True,
    testing=True,
  ),
  HexaOption(
    key="LaneOffsetEnabled",
    brands=edition.GWM_ONLY,
    group="direcao",
    title="posição na faixa pelo volante",
    description="Segure o volante 1,5 s para deslocar o carro na faixa; solte e ele mantém a partir de 35 km/h, mesmo se ensinou mais devagar.\n" +
                "Segure de volta ao centro para voltar. Zera ao desengatar.",
  ),
  HexaOption(
    key="AutoLaneChange",
    brands=edition.GWM_ONLY,
    group="direcao",
    title="troca de faixa pela seta",
    description="A partir de 60 km/h, a seta ligada troca de faixa sem empurrar o volante.\n" +
                "Uma troca por seta, e só com o ponto cego livre. Não use para virar.",
    restart_needed=True,
  ),
  HexaOption(
    key="AutoLaneChangeDelay",
    brands=edition.GWM_ONLY,
    group="direcao",
    title="tempo da seta até trocar",
    description="Quanto tempo a seta fica ligada antes de a troca começar.",
    kind="enum",
    choices=("1 s", "2 s", "3 s"),
    default=1,
    restart_needed=True,
  ),
  HexaOption(
    key="IsLdwEnabled",
    group="avisos",
    title="aviso de saída de faixa",
    description="Avisa quando o carro cruza uma linha sem a seta, a partir de 50 km/h.\n" +
                "Só alerta: não mexe no volante.",
    driving=False,
  ),
  HexaOption(
    key="RadarAlert",
    group="avisos",
    title="aviso de radar e lombada",
    description="Avisa com som antes dos radares, blitz e lombadas da lista, com a distância e o limite. Só avisa: não reduz sozinho.\n" +
                "Precisa de GPS. Pode faltar ponto. Corrija e adicione em Pontos.",
    driving=False,
    restart_needed=True,
  ),
  HexaOption(
    key="HexaSpeedLimit",
    group="velocidade",
    title="limite da via pelo mapa",
    description="Lê o limite da rua no mapa do OpenStreetMap, sem internet, e mostra no celular.\n" +
                "Avisar: um som quando passar do limite com a tolerância da lei (7 km/h até 100, 7% acima). Não muda a velocidade.\n" +
                "Ajustar (só no Haval): avisa e leva a velocidade do ACC ao limite a cada limite novo, um toque de + ou − por vez, até 110 km/h. " +
                "Qualquer pedal ou botão seu para o ajuste até o próximo limite.\n" +
                "Precisa do mapa da região baixado pelo celular. Vale a partir da próxima partida.",
    kind="enum",
    choices=("desligado", "mostrar", "avisar", "ajustar"),
    default=0,
    driving=False,
    testing=True,
  ),
  HexaOption(
    key="AutoResume",
    brands=edition.GWM_ONLY,
    group="velocidade",
    title="sair sozinho da parada",
    description="Parado atrás de um carro, o ACC sai quando ele anda. Só com openpilot ligado, você olhando para a frente, sem pedal e até 3 min parado.\n" +
                "Em teste: fique pronto para frear. Não lê o sinal: se o carro da frente sair no vermelho, o seu sai junto. Sem carro à frente, não sai.",
    restart_needed=True,
    testing=True,
  ),
  HexaOption(
    key="LeadClosing",
    brands=edition.GWM_ONLY,
    group="velocidade",
    title="frear antes com a distância da haval",
    description="Quando a distância medida pela Haval mostra o carro da frente parando antes do modelo perceber, " +
                "o comma começa a frear mais cedo e mais suave.\n" +
                "Em teste: fique pronto para frear. Precisa do longitudinal do comma. Só adiciona freio, e pode reduzir um pouco sem necessidade.",
    restart_needed=True,
    testing=True,
  ),
  HexaOption(
    key="StopFinish",
    brands=edition.GWM_ONLY,
    group="velocidade",
    title="parar de vez sem carro à frente",
    description="Sem carro à frente, quando o modelo freia até quase parar, o comma termina a parada e fica parado até você tocar no acelerador. " +
                "O modelo às vezes volta a acelerar no sinal vermelho.\n" +
                "Em teste: o carro não sai sozinho, e pode parar onde o modelo só ia reduzir. Precisa do longitudinal do comma.",
    restart_needed=True,
    testing=True,
  ),
  HexaOption(
    key="StopCloser",
    brands=edition.GWM_ONLY,
    group="velocidade",
    title="parar mais perto do carro da frente",
    description="No trânsito lento, o comma para até 1 m mais perto do carro da frente, mais perto dos 3 m do ACC da Haval. " +
                "Só abaixo de 30 km/h e só quando o radar da Haval e a câmera do comma medem o carro na mesma distância. " +
                "Numa freada forte ou quando um carro entra na frente, guarda a distância toda.\n" +
                "Em teste: fique pronto para frear. Precisa do longitudinal do comma.",
    restart_needed=True,
    testing=True,
  ),
  HexaOption(
    key="BumpSlowdown",
    brands=edition.GWM_ONLY,
    group="velocidade",
    title="reduzir na lombada",
    description="Antes de uma lombada marcada, o comma toca o − do ACC até a velocidade que você escolheu, de 30 km/h para cima. Abaixo disso, só avisa.\n" +
                "Em teste: fique pronto para frear. Precisa do aviso de radar e lombada ligado. Depois da lombada, suba com o + da alavanca.",
    restart_needed=True,
    testing=True,
  ),
  HexaOption(
    key="LeadDepartAlert",
    group="avisos",
    title="aviso de carro da frente saiu",
    description="Parado, um som avisa quando o carro da frente anda.\n" +
                "Só avisa: o carro não sai sozinho.",
    driving=False,
    restart_needed=True,
  ),
  HexaOption(
    key="AccLostAlert",
    brands=edition.GWM_ONLY,
    group="avisos",
    title="aviso de acc saiu sozinho",
    description="Com o openpilot na direção, um som avisa quando o ACC da Haval sai sem freio, acelerador ou alavanca.\n" +
                "Só avisa: a partir daí, a velocidade é sua.",
    driving=False,
    restart_needed=True,
    testing=True,
  ),
  HexaOption(
    key="HexaWebEnabled",
    group="celular",
    title="painel no celular",
    description="Abre uma página no celular, na mesma rede Wi-Fi ou no hotspot do comma,\n" +
                "para ver o estado e mudar estas opções. Só com celular pareado.",
    driving=False,
    phone_writable=False,
  ),
  HexaOption(
    key="HexaShareEnabled",
    brands=edition.GWM_ONLY,  # owner, 05/11: only the Haval; another brand only with its DBC reviewed by hand
    group="celular",
    title="enviar trechos aos desenvolvedores",
    description="Numa marca, o botão Enviar manda aos desenvolvedores 45 s do log daquele momento, para melhorar o LizPilot.\n" +
                "Vai o que o carro e o openpilot fizeram, e da rede CAN só o que o openpilot lê deste carro: " +
                "sem GPS, câmeras, áudio, VIN nem o número do comma.\n" +
                "Só envia a marca que você escolher, com o carro desligado e num Wi-Fi.",
    driving=False,
  ),
)

_BY_KEY = {o.key: o for o in OPTIONS}


def get_option(key: str) -> HexaOption | None:
  return _BY_KEY.get(key)


def is_on(params, key: str) -> bool:
  """A blocked option stays off even if it was turned on before it was blocked: the screen can't turn it off anymore."""
  opt = get_option(key)
  return opt is not None and not opt.blocked and bool(params.get_bool(key))


def options_in_group(group: str) -> list[HexaOption]:
  return [o for o in OPTIONS if o.group == group]


def locked_while_engaged(opt: HexaOption) -> bool:
  """The comma's screens lock it while openpilot is engaged: it changes the driving, or it asks for an onroad cycle, which
  stops controlsd and the rest for a second mid-drive (hardwared). Same rule as the comma's own toggles (toggles.py: "these
  toggles need restart, block while engaged"). The phone never asks for the cycle: there it counts from the next start."""
  return opt.driving or opt.restart_needed


def supported(opt: HexaOption, params) -> bool:
  """Shown only when this build has the option's param: a base without the feature has no key, and
  reading an unknown key raises. Stores without check_key (tests) know every key."""
  if opt.blocked:
    return False
  if not edition.allows(opt.brands, params):  # only tested on another brand's cars
    return False
  check = getattr(params, "check_key", None)
  if check is None:
    return True
  try:
    check(opt.key)
  except Exception:
    return False
  return True
