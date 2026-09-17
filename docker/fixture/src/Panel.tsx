export function Badge(props: { label: string }) {
  return <span>{props.label}</span>;
}

export function Panel(props: { label: string }) {
  return <div><Badge label={props.label} /></div>;
}
