/**
 * "New from template" — lists the shipped, code-defined pipeline templates;
 * picking one calls `POST /api/pipelines/from-template`.
 */

import { apiErrorCode } from "@core/api/public/client";
import { useCreatePipelineFromTemplate, usePipelineTemplates } from "@core/api/public/queries";
import { ErrorBanner } from "@shared/components/public/layout/error-banner";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@shared/components/ui/dialog";

function templateErrorMessage(err: unknown): string {
  const code = apiErrorCode(err);
  if (code === "name_taken") return "A pipeline with this name already exists.";
  if (code === "invalid_definition") return "That template's definition is invalid.";
  return "Couldn't create the pipeline.";
}

export function TemplateDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: () => void;
}) {
  const { data: templates } = usePipelineTemplates();
  const createFromTemplate = useCreatePipelineFromTemplate();

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent data-testid="pipeline-template-dialog">
        <DialogHeader>
          <DialogTitle>New from template</DialogTitle>
        </DialogHeader>
        <div className="flex flex-col gap-2">
          {templates.map((t) => (
            <button
              key={t.id}
              type="button"
              data-testid={`pipeline-template-${t.name}`}
              className="flex flex-col items-start rounded-md border border-border px-3 py-2 text-left text-sm hover:bg-accent disabled:opacity-50"
              disabled={createFromTemplate.isPending}
              onClick={() =>
                createFromTemplate.mutate(t.id, {
                  onSuccess: onCreated,
                })
              }
            >
              <span className="font-medium">{t.name}</span>
              {t.description && (
                <span className="text-muted-foreground text-xs mt-0.5">{t.description}</span>
              )}
            </button>
          ))}
        </div>
        {createFromTemplate.isError && (
          <ErrorBanner message={templateErrorMessage(createFromTemplate.error)} />
        )}
      </DialogContent>
    </Dialog>
  );
}
