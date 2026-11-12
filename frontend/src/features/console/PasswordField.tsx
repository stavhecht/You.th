/**
 * The desk's password input, with the same show-password eye the public forms use
 * (`RevealToggle`). Each field keeps its own shown state, as on the public site.
 */

import { useState, type InputHTMLAttributes } from "react";

import { RevealToggle } from "@/features/reader/PasswordParts";
import { FIELD } from "./controls";

export function PasswordField(props: Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "className">) {
  const [shown, setShown] = useState(false);
  return (
    <div className="relative mt-2">
      <input {...props} type={shown ? "text" : "password"} className={`${FIELD} pr-12`} />
      <RevealToggle shown={shown} onToggle={() => setShown((on) => !on)} />
    </div>
  );
}
