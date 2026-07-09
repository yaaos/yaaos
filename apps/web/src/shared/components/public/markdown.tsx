/**
 * Markdown — renders a model-generated artifact body as sanitized HTML.
 *
 * Bodies (pipeline stage artifacts, run failure text) are produced by an
 * LLM skill, never authored by a trusted party — `rehype-sanitize` runs on
 * every render (mandatory, not opt-in) so no raw HTML/script from a
 * generated body ever reaches the DOM.
 */

import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";

import { cn } from "@shared/utils/public/cn";

interface MarkdownProps {
  children: string;
  className?: string;
}

// No typography plugin installed — style block elements directly rather
// than relying on `prose` utilities that Tailwind wouldn't generate.
const MARKDOWN_CLASSES = cn(
  "text-sm text-foreground [&>*+*]:mt-3",
  "[&_h1]:text-lg [&_h1]:font-semibold [&_h2]:text-base [&_h2]:font-semibold [&_h3]:text-sm [&_h3]:font-semibold",
  "[&_ul]:list-disc [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5",
  "[&_a]:text-primary [&_a]:underline",
  "[&_code]:mono [&_code]:text-xs [&_code]:bg-muted [&_code]:rounded [&_code]:px-1 [&_code]:py-0.5",
  "[&_pre]:mono [&_pre]:text-xs [&_pre]:bg-muted [&_pre]:rounded [&_pre]:p-3 [&_pre]:overflow-x-auto",
  "[&_blockquote]:border-l-2 [&_blockquote]:border-border [&_blockquote]:pl-3 [&_blockquote]:text-muted-foreground",
);

export function Markdown({ children, className }: MarkdownProps) {
  return (
    <div className={cn(MARKDOWN_CLASSES, className)}>
      <ReactMarkdown rehypePlugins={[rehypeSanitize]}>{children}</ReactMarkdown>
    </div>
  );
}
