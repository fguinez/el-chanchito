// Whether the dashboard can save a credential right now, and why not. Saving
// seals to the database's active key, but only once the running scraper
// service confirms it holds that key: the row alone could be stale (mid
// rotation), another service's (key mismatch) or planted. So saving needs
// the service up, and a key the dashboard can seal to (v2: an older
// install's v1 key waits for its rotation). Pure: shared by GET
// /api/credentials (to disable Guardar) and PUT (to refuse with the same
// reason).

import type { ControlCredentialStatus } from "./control";
import { schemeForAlgorithm } from "./schemes";
import type { SealingKey } from "./seal";

export type SaveBlocker =
  | "service_unreachable"
  | "key_unavailable"
  | "key_mismatch"
  | "pending_unresolved"
  | "no_key"
  | "key_pending"
  | "key_needs_rotation";

export function saveBlocker(
  key: SealingKey | null,
  control: ControlCredentialStatus | null
): SaveBlocker | null {
  if (!control) return "service_unreachable";
  if (!control.key_available) return "key_unavailable";
  if (control.state === "pending_unresolved") return "pending_unresolved";
  if (control.key_mismatch) return "key_mismatch";
  if (!key) return "no_key";
  if (control.key_id !== key.keyId) return "key_pending";
  if (schemeForAlgorithm(key.algorithm) === null) return "key_needs_rotation";
  return null;
}

export const SAVE_BLOCKED_MESSAGES: Record<SaveBlocker, string> = {
  service_unreachable:
    "El servicio de scrapers no responde, así que no se puede confirmar su clave de cifrado. " +
    "Inicia el stack completo con `make dev` (o `make up`) y vuelve a intentarlo.",
  key_unavailable:
    "El servicio de scrapers no puede usar su archivo de clave (permisos, formato, o una clave " +
    "v1 antigua que esta base de datos ya no acepta como activa). Su log dice qué hacer " +
    "(`local/scrapers-dev.log` o `make logs-scrapers`).",
  key_mismatch:
    "La base de datos cifra las credenciales con la clave de otro servicio de scrapers (por " +
    "ejemplo, `make dev` y `make up` sobre la misma base), así que este no puede abrirlas ni " +
    "guardar nuevas. Deja corriendo un solo servicio de scrapers por base de datos (el que las " +
    "guardó) y detén el otro; no copies su archivo de clave, porque tras una rotación la copia " +
    "conserva la clave antigua. Si esa clave se perdió, ejecuta `make credentials-reset-key` " +
    "para partir de cero (habrá que volver a ingresarlas).",
  pending_unresolved:
    "Una rotación de la clave quedó a medias y esta base de datos no permite saber si se " +
    "completó (¿`DATABASE_URL` apunta a otra base, o la base se recreó?). El servicio guardó " +
    "las dos claves y no abre credenciales hasta resolverlo; el detalle está en su log. Si " +
    "`DATABASE_URL` es la correcta, ejecuta `make credentials-reset-key`: deja aparte el archivo " +
    "`.next` (sigue conteniendo una clave privada; bórralo cuando estés seguro) y habrá que " +
    "volver a ingresar las credenciales.",
  no_key:
    "El servicio de scrapers aún no ha publicado su clave de cifrado; suele tardar mientras " +
    "espera la base de datos. Vuelve a intentarlo en un minuto.",
  key_pending:
    "El servicio de scrapers todavía no confirma la clave de cifrado vigente (por ejemplo, " +
    "justo después de rotarla). Vuelve a intentarlo en un minuto.",
  key_needs_rotation:
    "La clave de cifrado del servicio de scrapers usa un esquema anterior (v1), al que ya no " +
    "se guardan credenciales. El servicio la rota sola a una clave X-Wing (v2) en su próxima " +
    "revisión (suele ser en cerca de un minuto; si falla, reintenta cada hora y su log dice " +
    "por qué), sin que haya que volver a ingresar nada; para hacerlo ahora, ejecuta " +
    "`make credentials-rotate` (o `make credentials-rotate STACK=up`). Rotar necesita " +
    "bloquear el archivo de clave, así que en Windows nativo no ocurre: corre el servicio en " +
    "macOS, Linux o WSL2.",
};

/** What PUT answers when the key changed between sealing and storing, twice. */
export const KEY_CHANGED_MESSAGE =
  "La clave de cifrado cambió mientras guardabas (se rotó). Vuelve a intentarlo.";
