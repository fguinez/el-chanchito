"use client";

import { useState } from "react";
import { Pencil } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  ACCOUNT_NAME_MAX,
  INSTITUTION_KINDS,
  INSTITUTION_KIND_LABELS,
  INSTITUTION_NAME_MAX,
  INSTITUTION_URL_MAX,
  isCountryCode,
  isHttpUrl,
} from "@/lib/management";
import type { ApiInstitution } from "@/components/institutions/shared";
import {
  SELECT_CLASS,
  patchJson,
} from "@/components/institutions/management-shared";

interface AccountDraft {
  id: string;
  name: string;
  draft: string;
  error: string | null;
  saving: boolean;
}

/** The institution's accounts (enrollments), from its products, in order. */
function accountsOf(institution: ApiInstitution): AccountDraft[] {
  const seen = new Map<string, AccountDraft>();
  for (const p of institution.products) {
    if (!seen.has(p.accountId)) {
      seen.set(p.accountId, {
        id: p.accountId,
        name: p.accountName,
        draft: p.accountName,
        error: null,
        saving: false,
      });
    }
  }
  return [...seen.values()];
}

/**
 * "Editar" button + dialog for one institution: its name, kind, country and
 * site, plus a rename field per account. The slug is shown read-only, since
 * scrapers resolve the institution by it. `onSaved` runs after any save.
 */
export function InstitutionEditDialog({
  institution,
  onSaved,
}: {
  institution: ApiInstitution;
  onSaved: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState(institution.name);
  const [kind, setKind] = useState(institution.kind);
  const [country, setCountry] = useState(institution.country ?? "");
  const [url, setUrl] = useState(institution.url ?? "");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [accounts, setAccounts] = useState<AccountDraft[]>([]);

  function handleOpenChange(next: boolean) {
    setOpen(next);
    if (!next) return;
    // Start every opening from the institution as it is now.
    setName(institution.name);
    setKind(institution.kind);
    setCountry(institution.country ?? "");
    setUrl(institution.url ?? "");
    setError(null);
    setAccounts(accountsOf(institution));
  }

  async function saveInstitution(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) {
      setError("El nombre es obligatorio");
      return;
    }

    const body: Record<string, unknown> = {};
    if (trimmed !== institution.name) body.name = trimmed;
    if (kind !== institution.kind) body.kind = kind;
    const countryValue = country.trim().toUpperCase() || null;
    if (countryValue != null && !isCountryCode(countryValue)) {
      setError("El país debe ser un código de dos letras, como CL");
      return;
    }
    if (countryValue !== institution.country) body.country = countryValue;
    const urlValue = url.trim() || null;
    if (urlValue != null && !isHttpUrl(urlValue)) {
      setError("El sitio web debe ser una dirección http(s)");
      return;
    }
    if (urlValue !== institution.url) body.url = urlValue;
    if (Object.keys(body).length === 0) {
      setOpen(false);
      return;
    }

    setSaving(true);
    setError(null);
    const result = await patchJson(`/api/institutions/${institution.slug}`, body);
    setSaving(false);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    setOpen(false);
    onSaved();
  }

  function updateAccount(id: string, patch: Partial<AccountDraft>) {
    setAccounts((prev) =>
      prev.map((a) => (a.id === id ? { ...a, ...patch } : a))
    );
  }

  async function renameAccount(account: AccountDraft) {
    const trimmed = account.draft.trim();
    if (!trimmed) {
      updateAccount(account.id, { error: "El nombre es obligatorio" });
      return;
    }
    if (trimmed === account.name) return;
    updateAccount(account.id, { saving: true, error: null });
    const result = await patchJson(
      `/api/accounts/${account.id}`,
      { name: trimmed },
      { name: `${institution.name} ya tiene una cuenta con ese nombre.` }
    );
    if (!result.ok) {
      updateAccount(account.id, { saving: false, error: result.error });
      return;
    }
    updateAccount(account.id, { saving: false, name: trimmed, draft: trimmed });
    onSaved();
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <Pencil className="h-4 w-4" />
          Editar
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Editar institución</DialogTitle>
          <DialogDescription>
            Identificador: <code>{institution.slug}</code>. Los scrapers
            reconocen la institución por él, así que no se puede cambiar.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={saveInstitution} className="space-y-4">
          <div className="space-y-1.5">
            <label htmlFor="institution-name" className="text-sm font-medium">
              Nombre
            </label>
            <Input
              id="institution-name"
              value={name}
              maxLength={INSTITUTION_NAME_MAX}
              onChange={(e) => setName(e.target.value)}
            />
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <label htmlFor="institution-kind" className="text-sm font-medium">
                Tipo
              </label>
              <select
                id="institution-kind"
                className={SELECT_CLASS}
                value={kind}
                onChange={(e) => setKind(e.target.value)}
              >
                {INSTITUTION_KINDS.map((k) => (
                  <option key={k} value={k}>
                    {INSTITUTION_KIND_LABELS[k]}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1.5">
              <label
                htmlFor="institution-country"
                className="text-sm font-medium"
              >
                País
              </label>
              <Input
                id="institution-country"
                value={country}
                maxLength={2}
                placeholder="CL"
                onChange={(e) => setCountry(e.target.value)}
              />
            </div>
          </div>

          <div className="space-y-1.5">
            <label htmlFor="institution-url" className="text-sm font-medium">
              Sitio web
            </label>
            <Input
              id="institution-url"
              type="url"
              value={url}
              maxLength={INSTITUTION_URL_MAX}
              placeholder="https://"
              onChange={(e) => setUrl(e.target.value)}
            />
          </div>

          {error && <p className="text-sm text-destructive">{error}</p>}
          <DialogFooter>
            <Button type="submit" disabled={saving}>
              {saving ? "Guardando…" : "Guardar cambios"}
            </Button>
          </DialogFooter>
        </form>

        {accounts.length > 0 && (
          <>
            <Separator />
            <div className="space-y-3">
              <div>
                <h3 className="text-sm font-medium">
                  {accounts.length === 1 ? "Cuenta" : "Cuentas"}
                </h3>
                <p className="text-xs text-muted-foreground">
                  Tu enrolamiento en la institución. Un nombre distinto de
                  “Personal” aparece junto a cada producto.
                </p>
              </div>
              {accounts.map((account) => (
                <form
                  key={account.id}
                  className="space-y-1"
                  onSubmit={(e) => {
                    e.preventDefault();
                    renameAccount(account);
                  }}
                >
                  <div className="flex gap-2">
                    <Input
                      aria-label="Nombre de la cuenta"
                      value={account.draft}
                      maxLength={ACCOUNT_NAME_MAX}
                      onChange={(e) =>
                        updateAccount(account.id, {
                          draft: e.target.value,
                          error: null,
                        })
                      }
                    />
                    <Button
                      type="submit"
                      variant="outline"
                      disabled={
                        account.saving || account.draft.trim() === account.name
                      }
                    >
                      {account.saving ? "Guardando…" : "Renombrar"}
                    </Button>
                  </div>
                  {account.error && (
                    <p className="text-sm text-destructive">{account.error}</p>
                  )}
                </form>
              ))}
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
