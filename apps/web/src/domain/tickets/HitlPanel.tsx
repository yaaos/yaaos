/**
 * HITL prompt renderer — E2a.4 discriminated-union schema.
 *
 * Backend HITL commands populate `question_payload` with a
 * `{kind: "choice" | "text" | "form", title, body, …}` shape. When the
 * `kind` is missing or unrecognized, the fallback renderer shows the raw
 * `body` markdown + a free-text response box so legacy / unknown prompts
 * still work.
 *
 * Submit invokes `useHitlRespond(ticket_id).mutate(response)` — caller
 * passes the ticket id; the panel shapes the response per the discriminator.
 */

import { Button } from "@shared/components/ui/button";
import { Input } from "@shared/components/ui/input";
import { Textarea } from "@shared/components/ui/textarea";
import { cn } from "@shared/utils";
import { useState } from "react";

export interface ChoicePrompt {
  kind: "choice";
  title: string;
  body: string;
  options: Array<{ value: string; label: string; variant?: "default" | "destructive" }>;
}

export interface TextPrompt {
  kind: "text";
  title: string;
  body: string;
  placeholder?: string;
  multiline?: boolean;
}

export interface FormField {
  name: string;
  label: string;
  type: "text" | "textarea" | "select";
  options?: Array<{ value: string; label: string }>;
  required?: boolean;
}

export interface FormPrompt {
  kind: "form";
  title: string;
  body: string;
  fields: FormField[];
}

export type HitlPrompt = ChoicePrompt | TextPrompt | FormPrompt;

interface HitlPanelProps {
  payload: Record<string, unknown>;
  onSubmit: (response: Record<string, unknown>) => void;
  pending?: boolean;
}

export function HitlPanel({ payload, onSubmit, pending }: HitlPanelProps) {
  const kind = payload.kind as string | undefined;
  const title = (payload.title as string | undefined) ?? "yaaos needs a decision";
  const body = (payload.body as string | undefined) ?? "";

  if (kind === "choice") {
    return (
      <ChoiceBody
        payload={payload as unknown as ChoicePrompt}
        onSubmit={onSubmit}
        pending={pending}
      />
    );
  }
  if (kind === "text") {
    return (
      <TextBody payload={payload as unknown as TextPrompt} onSubmit={onSubmit} pending={pending} />
    );
  }
  if (kind === "form") {
    return (
      <FormBody payload={payload as unknown as FormPrompt} onSubmit={onSubmit} pending={pending} />
    );
  }

  return <FallbackBody title={title} body={body} onSubmit={onSubmit} pending={pending} />;
}

function Header({ title, body }: { title: string; body: string }) {
  return (
    <header className="mb-3">
      <h3 className="text-base font-semibold">{title}</h3>
      {body && <p className="text-sm text-muted-foreground mt-1 whitespace-pre-wrap">{body}</p>}
    </header>
  );
}

function ChoiceBody({
  payload,
  onSubmit,
  pending,
}: {
  payload: ChoicePrompt;
  onSubmit: (r: Record<string, unknown>) => void;
  pending?: boolean;
}) {
  return (
    <div
      data-testid="hitl-panel"
      aria-live="assertive"
      className="rounded-md border border-warning/40 bg-warning/5 p-4"
    >
      <Header title={payload.title} body={payload.body} />
      <div className="flex flex-wrap gap-2" data-testid="hitl-choice-options">
        {payload.options.map((o) => (
          <Button
            key={o.value}
            variant={o.variant === "destructive" ? "destructive" : "default"}
            onClick={() => onSubmit({ choice: o.value })}
            disabled={pending}
            data-testid={`hitl-choice-${o.value}`}
          >
            {o.label}
          </Button>
        ))}
      </div>
    </div>
  );
}

function TextBody({
  payload,
  onSubmit,
  pending,
}: {
  payload: TextPrompt;
  onSubmit: (r: Record<string, unknown>) => void;
  pending?: boolean;
}) {
  const [value, setValue] = useState("");
  const submit = () => {
    if (value.trim()) onSubmit({ text: value });
  };
  return (
    <form
      data-testid="hitl-panel"
      aria-live="assertive"
      className="rounded-md border border-warning/40 bg-warning/5 p-4"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <Header title={payload.title} body={payload.body} />
      {payload.multiline ? (
        <Textarea
          data-testid="hitl-text-input"
          placeholder={payload.placeholder}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          rows={3}
        />
      ) : (
        <Input
          data-testid="hitl-text-input"
          placeholder={payload.placeholder}
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
      )}
      <div className="flex justify-end mt-3">
        <Button type="submit" disabled={pending || !value.trim()}>
          Submit
        </Button>
      </div>
    </form>
  );
}

function FormBody({
  payload,
  onSubmit,
  pending,
}: {
  payload: FormPrompt;
  onSubmit: (r: Record<string, unknown>) => void;
  pending?: boolean;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const setValue = (name: string, v: string) => setValues((prev) => ({ ...prev, [name]: v }));
  const canSubmit =
    payload.fields
      .filter((f) => f.required)
      .every((f) => (values[f.name] ?? "").trim().length > 0) && !pending;
  return (
    <form
      data-testid="hitl-panel"
      aria-live="assertive"
      className="rounded-md border border-warning/40 bg-warning/5 p-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (canSubmit) onSubmit(values);
      }}
    >
      <Header title={payload.title} body={payload.body} />
      <div className="flex flex-col gap-3">
        {payload.fields.map((f) => (
          <FieldRow key={f.name} field={f} value={values[f.name] ?? ""} onChange={setValue} />
        ))}
      </div>
      <div className="flex justify-end mt-3">
        <Button type="submit" disabled={!canSubmit}>
          Submit
        </Button>
      </div>
    </form>
  );
}

function FieldRow({
  field,
  value,
  onChange,
}: {
  field: FormField;
  value: string;
  onChange: (name: string, v: string) => void;
}) {
  const label = (
    <label
      className="text-xs font-medium text-muted-foreground"
      htmlFor={`hitl-field-${field.name}`}
    >
      {field.label}
      {field.required && <span className="text-destructive ml-0.5">*</span>}
    </label>
  );
  const id = `hitl-field-${field.name}`;
  if (field.type === "textarea") {
    return (
      <div className="flex flex-col gap-1">
        {label}
        <Textarea
          id={id}
          data-testid={`hitl-field-${field.name}`}
          value={value}
          onChange={(e) => onChange(field.name, e.target.value)}
          rows={3}
        />
      </div>
    );
  }
  if (field.type === "select") {
    return (
      <div className="flex flex-col gap-1">
        {label}
        <select
          id={id}
          data-testid={`hitl-field-${field.name}`}
          value={value}
          onChange={(e) => onChange(field.name, e.target.value)}
          className={cn(
            "h-9 px-2 rounded-md border border-input bg-background text-sm",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          )}
        >
          <option value="">—</option>
          {(field.options ?? []).map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-1">
      {label}
      <Input
        id={id}
        data-testid={`hitl-field-${field.name}`}
        value={value}
        onChange={(e) => onChange(field.name, e.target.value)}
      />
    </div>
  );
}

function FallbackBody({
  title,
  body,
  onSubmit,
  pending,
}: {
  title: string;
  body: string;
  onSubmit: (r: Record<string, unknown>) => void;
  pending?: boolean;
}) {
  const [value, setValue] = useState("");
  return (
    <form
      data-testid="hitl-panel"
      data-hitl-fallback="true"
      aria-live="assertive"
      className="rounded-md border border-warning/40 bg-warning/5 p-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (value.trim()) onSubmit({ text: value });
      }}
    >
      <Header title={title} body={body} />
      <Textarea
        data-testid="hitl-text-input"
        placeholder="Type your response…"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        rows={3}
      />
      <div className="flex justify-end mt-3">
        <Button type="submit" disabled={pending || !value.trim()}>
          Submit
        </Button>
      </div>
    </form>
  );
}
