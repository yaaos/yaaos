import {
  type Lesson,
  useCreateLesson,
  useDeleteLesson,
  useGithubRepositories,
  useLessons,
} from "@core/api";
import { PageHeader } from "@shared/components/layout";
import { Button } from "@shared/components/ui/button";
import { Input } from "@shared/components/ui/input";
import { Label } from "@shared/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@shared/components/ui/select";
import { Textarea } from "@shared/components/ui/textarea";
import { useState } from "react";

export function LessonsPage() {
  const { data: lessons } = useLessons();
  const { data: repos } = useGithubRepositories();
  const create = useCreateLesson();
  const remove = useDeleteLesson();

  const [repoExternalId, setRepoExternalId] = useState<string>("");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");

  const repoNamesFromLessons = new Set((lessons ?? []).map((l) => l.repo_external_id));
  const repoNamesFromInstall = new Set(
    (repos?.repositories ?? []).map((r) => r.full_name).filter(Boolean) as string[],
  );
  const pickerOptions = Array.from(
    new Set([...repoNamesFromInstall, ...repoNamesFromLessons]),
  ).sort();

  return (
    <div className="mx-auto max-w-[900px] flex flex-col gap-4 p-6">
      <PageHeader
        title="Lessons"
        subtitle="Repository-scoped notes the reviewer agent consults during reviews."
      />

      <section className="rounded-lg border border-border bg-card">
        <header className="border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold">Add a lesson</h2>
        </header>
        <div className="px-4 py-4">
          <form
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              if (!repoExternalId || !title.trim() || !body.trim()) return;
              create.mutate(
                { repo_external_id: repoExternalId, title: title.trim(), body: body.trim() },
                {
                  onSuccess: () => {
                    setTitle("");
                    setBody("");
                  },
                },
              );
            }}
          >
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="lesson-repo">Repo</Label>
              <Select value={repoExternalId} onValueChange={setRepoExternalId}>
                <SelectTrigger id="lesson-repo" data-testid="lesson-repo">
                  <SelectValue placeholder="(select a repo)" />
                </SelectTrigger>
                <SelectContent>
                  {pickerOptions.map((name) => (
                    <SelectItem key={name} value={name}>
                      {name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {pickerOptions.length === 0 && (
                <p className="text-muted-foreground text-xs">
                  No repos yet. Install the GitHub App on at least one repo to populate this list.
                </p>
              )}
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="lesson-title">Title</Label>
              <Input
                id="lesson-title"
                data-testid="lesson-title"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="One-line summary"
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="lesson-body">Body</Label>
              <Textarea
                id="lesson-body"
                data-testid="lesson-body"
                value={body}
                onChange={(e) => setBody(e.target.value)}
                placeholder="≤1000 chars"
                maxLength={1000}
                rows={4}
              />
            </div>
            <div>
              <Button type="submit" data-testid="lesson-save" disabled={create.isPending}>
                {create.isPending ? "Saving…" : "Save"}
              </Button>
            </div>
            {create.isError && (
              <p className="text-destructive text-xs" data-testid="lesson-error">
                {(create.error as Error).message}
              </p>
            )}
          </form>
        </div>
      </section>

      <section className="rounded-lg border border-border bg-card">
        <header className="border-b border-border px-4 py-3">
          <h2 className="text-sm font-semibold">Lessons</h2>
        </header>
        <div className="px-4 py-4">
          <ul className="flex flex-col gap-2" data-testid="lessons-list">
            {lessons?.map((l: Lesson) => (
              <li key={l.id} className="rounded-md border border-border bg-background p-3 text-sm">
                <div className="flex items-center gap-2">
                  <span className="font-medium flex-1">{l.title}</span>
                  <span className="font-mono text-muted-foreground text-xs">
                    {l.repo_external_id}
                  </span>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => remove.mutate(l.id)}
                    className="text-destructive hover:text-destructive"
                  >
                    Delete
                  </Button>
                </div>
                <p className="text-muted-foreground mt-1 whitespace-pre-wrap text-xs">{l.body}</p>
              </li>
            ))}
            {lessons && lessons.length === 0 && (
              <li className="text-muted-foreground text-sm">No lessons yet.</li>
            )}
          </ul>
        </div>
      </section>
    </div>
  );
}
