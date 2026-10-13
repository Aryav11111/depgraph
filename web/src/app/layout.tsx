import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";

import { ApiStatus } from "@/components/api-status";

import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "depgraph: dependency vulnerability explorer",
  description:
    "Upload a package-lock.json to see vulnerable and malicious packages, every dependency path to them, and the smallest fix.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}>
      <body className="flex min-h-full flex-col">
        <header className="border-b border-zinc-200 dark:border-zinc-800">
          <div className="mx-auto flex w-full max-w-5xl items-center justify-between gap-4 px-4 py-3 sm:px-6">
            <Link href="/" className="font-mono text-lg font-semibold tracking-tight">
              depgraph
            </Link>
            <nav className="flex items-center gap-4 text-sm">
              <a
                href="https://github.com/Aryav11111/depgraph"
                className="text-zinc-600 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
              >
                GitHub
              </a>
            </nav>
          </div>
        </header>
        {children}
        <footer className="border-t border-zinc-200 dark:border-zinc-800">
          <div className="mx-auto flex w-full max-w-5xl flex-wrap items-center justify-between gap-3 px-4 py-4 text-xs text-zinc-600 dark:text-zinc-400 sm:px-6">
            <span>Advisories from OSV.dev (GitHub Advisory Database + OpenSSF malicious packages).</span>
            <ApiStatus />
          </div>
        </footer>
      </body>
    </html>
  );
}
