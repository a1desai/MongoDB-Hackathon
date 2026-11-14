import type { Metadata, Viewport } from "next";
import "mapbox-gl/dist/mapbox-gl.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "GridLock — governed agents for a heat wave",
  description:
    "Governed AI agents keep Toronto's power on through a heat wave. Every action is identity-bound, policy-checked, ledgered in MongoDB Atlas, and can be halted or rolled back.",
};

export const viewport: Viewport = {
  themeColor: "#101412",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="h-full overflow-hidden bg-background text-foreground">{children}</body>
    </html>
  );
}
