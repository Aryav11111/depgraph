import Markdown from "react-markdown";

import { EXTERNAL_LINK, safeHttpUrl } from "@/lib/safe-url";

/**
 * Renders advisory `details` (third-party Markdown, sometimes written about
 * malware by people quoting malware) with defence in depth:
 *
 * - an element **allowlist**: text formatting, lists, code, quotes, links; no
 *   images (no tracking pixels), no raw HTML (`skipHtml`), nothing else;
 * - react-markdown's default `urlTransform` already neutralises `javascript:`;
 *   links are then re-checked by `safeHttpUrl` (http/https only, no
 *   `user@host` spoofs) and open in a new tab with `noopener noreferrer nofollow`.
 *   Anything else renders as plain text.
 */
const ALLOWED = [
  "p", "a", "strong", "em", "del", "code", "pre", "ul", "ol", "li",
  "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "br",
];  // prettier-ignore

export function AdvisoryMarkdown({ children }: { children: string }) {
  return (
    <div className="flex flex-col gap-3 text-sm leading-relaxed text-zinc-700 dark:text-zinc-300 [&_code]:rounded [&_code]:bg-zinc-100 [&_code]:px-1 [&_code]:font-mono [&_code]:text-[0.9em] dark:[&_code]:bg-zinc-800 [&_ol]:list-decimal [&_ol]:pl-5 [&_pre]:overflow-x-auto [&_pre]:rounded-lg [&_pre]:bg-zinc-100 [&_pre]:p-3 dark:[&_pre]:bg-zinc-900 [&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_ul]:list-disc [&_ul]:pl-5 [&_blockquote]:border-l-2 [&_blockquote]:border-zinc-300 [&_blockquote]:pl-3 [&_h1]:font-semibold [&_h2]:font-semibold [&_h3]:font-semibold [&_h4]:font-semibold">
      <Markdown
        allowedElements={ALLOWED}
        unwrapDisallowed
        skipHtml
        components={{
          a: ({ href, children: text }) => {
            const safe = safeHttpUrl(href);
            return safe ? (
              <a href={safe} {...EXTERNAL_LINK} className="text-indigo-600 underline-offset-2 hover:underline dark:text-indigo-400">
                {text}
              </a>
            ) : (
              <span>{text}</span>
            );
          },
          // Headings inside an advisory shouldn't outrank the page's own.
          h1: ({ children: c }) => <p className="font-semibold">{c}</p>,
          h2: ({ children: c }) => <p className="font-semibold">{c}</p>,
        }}
      >
        {children}
      </Markdown>
    </div>
  );
}
