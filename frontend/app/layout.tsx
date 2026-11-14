import type { Metadata, Viewport } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import "mapbox-gl/dist/mapbox-gl.css";
import "./globals.css";

const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
});

const jetbrains = JetBrains_Mono({
  variable: "--font-jetbrains",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Cooldown — governed agents for a heat wave",
  description:
    "Governed AI agents keep Toronto's power on through a heat wave. Every action is identity-bound, policy-checked, ledgered in MongoDB Atlas, and can be halted or rolled back.",
};

export const viewport: Viewport = {
  themeColor: "#04060a",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${inter.variable} ${jetbrains.variable} h-full antialiased`}>
      <body className="h-full overflow-hidden bg-background text-foreground">{children}</body>
    </html>
  );
}
