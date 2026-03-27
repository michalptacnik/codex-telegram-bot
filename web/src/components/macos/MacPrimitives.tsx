import type { ReactNode } from 'react';
import { Search } from 'lucide-react';

function cn(...parts: Array<string | false | null | undefined>) {
  return parts.filter(Boolean).join(' ');
}

export function MacPage({
  eyebrow,
  title,
  description,
  actions,
  children,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="mac-page">
      <div className="mac-page-header">
        <div>
          {eyebrow ? <p className="mac-page-eyebrow">{eyebrow}</p> : null}
          <h1 className="mac-page-title">{title}</h1>
          {description ? <p className="mac-page-description">{description}</p> : null}
        </div>
        {actions ? <div className="mac-page-actions">{actions}</div> : null}
      </div>
      {children}
    </div>
  );
}

export function MacPanel({
  title,
  detail,
  children,
  className,
}: {
  title?: string;
  detail?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cn('mac-panel', className)}>
      {title || detail ? (
        <div className="mac-panel-header">
          {title ? <h2 className="mac-panel-title">{title}</h2> : null}
          {detail ? <p className="mac-panel-detail">{detail}</p> : null}
        </div>
      ) : null}
      {children}
    </section>
  );
}

export function MacStat({
  label,
  value,
  detail,
}: {
  label: string;
  value: string;
  detail?: string;
}) {
  return (
    <div className="mac-stat">
      <p className="mac-stat-label">{label}</p>
      <p className="mac-stat-value">{value}</p>
      {detail ? <p className="mac-stat-detail">{detail}</p> : null}
    </div>
  );
}

export function MacBadge({
  children,
  tone = 'neutral',
}: {
  children: ReactNode;
  tone?: 'neutral' | 'success' | 'warning' | 'danger' | 'accent';
}) {
  return <span className={cn('mac-badge', `mac-badge-${tone}`)}>{children}</span>;
}

export function MacEmptyState({
  icon,
  title,
  description,
}: {
  icon?: ReactNode;
  title: string;
  description: string;
}) {
  return (
    <div className="mac-empty-state">
      {icon ? <div className="mac-empty-icon">{icon}</div> : null}
      <h3 className="mac-empty-title">{title}</h3>
      <p className="mac-empty-description">{description}</p>
    </div>
  );
}

export function MacSearchField({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
}) {
  return (
    <label className="mac-search">
      <Search className="mac-search-icon" />
      <input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        className="mac-search-input"
      />
    </label>
  );
}
