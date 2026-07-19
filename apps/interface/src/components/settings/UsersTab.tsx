// Settings → Users: the install's staff list. Admin-only (the whole page is), so every row is
// fully editable here: role, enable/disable, password reset, delete. Two guarded operations
// surface the server's honest 409s verbatim — a taken username and the last-admin rule (the
// install must never strand itself without an active admin).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, ShieldCheck, UserX } from "lucide-react";
import { useState } from "react";
import { type AuthUser, type Role, api } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { notify } from "../../lib/notify";
import { TimeAgo } from "../TimeAgo";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  ErrorBanner,
  Field,
  Input,
  SectionHeading,
  Select,
  Spinner,
  Table,
  Td,
  Th,
} from "../ui";
import { Avatar, AvatarFallback, AvatarImage } from "../ui/avatar";

const ROLES: Role[] = ["viewer", "editor", "admin"];

function initialsOf(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  return (
    parts
      .slice(0, 2)
      .map((p) => p[0]?.toUpperCase() ?? "")
      .join("") || "?"
  );
}

export function UsersTab() {
  const qc = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: () => api.listUsers() });
  const invalidate = () => qc.invalidateQueries({ queryKey: ["users"] });

  if (users.isError) return <ErrorBanner error={users.error} />;
  if (!users.data) return <Spinner label="Loading users…" />;

  return (
    <div className="space-y-4">
      <Card className="p-4">
        <SectionHeading>People</SectionHeading>
        <p className="mt-1 text-xs text-muted-foreground">
          Everyone with access to this install. Admins own it, editors build agents, viewers use
          them. Sign-in providers can auto-create accounts — configure that under Sign-in.
        </p>
        <div className="mt-3 overflow-x-auto">
          <Table>
            <thead>
              <tr>
                <Th>User</Th>
                <Th>Role</Th>
                <Th>Status</Th>
                <Th>Last sign-in</Th>
                <Th> </Th>
              </tr>
            </thead>
            <tbody>
              {users.data.users.map((u) => (
                <UserRow key={u.id} user={u} onChanged={invalidate} />
              ))}
            </tbody>
          </Table>
        </div>
      </Card>

      <CreateUserCard onCreated={invalidate} />
    </div>
  );
}

function UserRow({ user, onChanged }: { user: AuthUser; onChanged: () => void }) {
  const { user: me, refresh } = useAuth();
  const isSelf = me?.id === user.id;
  const [deleting, setDeleting] = useState(false);
  const [resetting, setResetting] = useState(false);

  const patch = useMutation({
    mutationFn: (body: { role?: Role; disabled?: boolean }) => api.updateUser(user.id, body),
    onSuccess: () => {
      onChanged();
      if (isSelf) refresh(); // own role change re-renders the whole shell's gates
    },
    onError: (err) => notify.error(err instanceof Error ? err.message : String(err)),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteUser(user.id),
    onSuccess: () => {
      notify.success(`Deleted ${user.username}`);
      onChanged();
    },
    onError: (err) => notify.error(err instanceof Error ? err.message : String(err)),
  });

  return (
    <tr>
      <Td>
        <div className="flex min-w-0 items-center gap-2.5">
          <Avatar size="sm">
            {user.avatar_url && <AvatarImage src={user.avatar_url} alt="" />}
            <AvatarFallback className="bg-primary text-[10px] font-semibold text-primary-foreground">
              {initialsOf(user.display_name || user.username)}
            </AvatarFallback>
          </Avatar>
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 text-sm text-foreground">
              {user.display_name}
              {user.role === "admin" && (
                <ShieldCheck size={13} className="text-primary" aria-label="Admin" />
              )}
              {isSelf && <Badge tone="blue">you</Badge>}
            </div>
            <div className="mono text-[11px] text-muted-foreground">
              @{user.username}
              {user.email ? ` · ${user.email}` : ""}
              {!user.has_password ? " · SSO-only" : ""}
            </div>
          </div>
        </div>
      </Td>
      <Td>
        <Select
          aria-label={`Role for ${user.username}`}
          value={user.role}
          onChange={(e) => patch.mutate({ role: e.target.value as Role })}
        >
          {ROLES.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </Select>
      </Td>
      <Td>
        {user.disabled ? <Badge tone="rose">disabled</Badge> : <Badge tone="emerald">active</Badge>}
      </Td>
      <Td>
        {user.last_login_at ? (
          <TimeAgo iso={user.last_login_at} />
        ) : (
          <span className="text-xs text-muted-foreground">never</span>
        )}
      </Td>
      <Td>
        <div className="flex justify-end gap-1.5">
          <Button onClick={() => setResetting(true)}>Reset password</Button>
          <Button onClick={() => patch.mutate({ disabled: !user.disabled })}>
            {user.disabled ? "Enable" : "Disable"}
          </Button>
          <Button variant="danger" onClick={() => setDeleting(true)}>
            <UserX size={14} />
          </Button>
        </div>
      </Td>

      {resetting && <ResetPasswordDialog user={user} onClose={() => setResetting(false)} />}
      {deleting && (
        <ConfirmDialog
          title={`Delete ${user.username}?`}
          message="Their sessions and API keys are revoked immediately. Their chats and run history stay (unowned)."
          confirmLabel="Delete user"
          onConfirm={() => {
            remove.mutate();
            setDeleting(false);
          }}
          onCancel={() => setDeleting(false)}
        />
      )}
    </tr>
  );
}

function ResetPasswordDialog({ user, onClose }: { user: AuthUser; onClose: () => void }) {
  const [password, setPassword] = useState("");
  const reset = useMutation({
    mutationFn: () => api.resetUserPassword(user.id, password),
    onSuccess: () => {
      notify.success(`Password reset for ${user.username} — their sessions were signed out`);
      onClose();
    },
    onError: (err) => notify.error(err instanceof Error ? err.message : String(err)),
  });
  return (
    <ConfirmDialog
      title={`Reset password for ${user.username}`}
      message={
        <div className="space-y-2">
          <p>Set a new password and hand it over out-of-band. Every session they have ends now.</p>
          <Input
            type="password"
            aria-label="New password"
            placeholder="New password (min 8 characters)"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
      }
      confirmLabel="Reset password"
      onConfirm={() => {
        if (password.length >= 8) reset.mutate();
        else notify.error("Password must be at least 8 characters");
      }}
      onCancel={onClose}
    />
  );
}

function CreateUserCard({ onCreated }: { onCreated: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("viewer");

  const create = useMutation({
    mutationFn: () =>
      api.createUser({
        username: username.trim(),
        password: password || undefined,
        email: email.trim() || undefined,
        role,
      }),
    onSuccess: (u) => {
      notify.success(`Created ${u.username}`);
      setUsername("");
      setPassword("");
      setEmail("");
      setRole("viewer");
      onCreated();
    },
  });
  const canCreate =
    username.trim() !== "" && (password === "" || password.length >= 8) && !create.isPending;

  return (
    <Card className="p-4">
      <SectionHeading>Add a user</SectionHeading>
      <p className="mt-1 text-xs text-muted-foreground">
        Leave the password blank for an SSO-only account — set their email so their first provider
        sign-in links to this account.
      </p>
      <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <Field label="Username">
          <Input
            value={username}
            placeholder="lowercase handle"
            onChange={(e) => setUsername(e.target.value)}
          />
        </Field>
        <Field label="Password (optional)">
          <Input
            type="password"
            value={password}
            placeholder="blank = SSO-only"
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        <Field label="Email (optional)">
          <Input
            value={email}
            placeholder="links SSO sign-ins"
            onChange={(e) => setEmail(e.target.value)}
          />
        </Field>
        <Field label="Role">
          <Select value={role} onChange={(e) => setRole(e.target.value as Role)}>
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </Select>
        </Field>
      </div>
      <div className="mt-3 flex items-center gap-3">
        <Button
          variant="primary"
          disabled={!canCreate}
          onClick={() => canCreate && create.mutate()}
        >
          <Plus size={14} />
          Create user
        </Button>
        <ErrorBanner error={create.error} />
      </div>
    </Card>
  );
}
