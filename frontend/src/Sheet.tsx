import { X } from 'lucide-react'
import { useEffect, useId, useRef, type ReactNode } from 'react'
import { Button } from './components/ui'

export function Sheet({ title, onClose, children }: {
  title: string; onClose: () => void; children: ReactNode
}) {
  const ref = useRef<HTMLDialogElement>(null)
  const label = useId()
  useEffect(() => {
    if (ref.current && !ref.current.open) ref.current.showModal()
  }, [])

  return <dialog ref={ref} aria-labelledby={label} className="cockpit-sheet" onClose={onClose}
    onClick={(event) => { if (event.target === event.currentTarget) ref.current?.close() }}>
    <div className="cockpit-sheet-body">
      <header className="sheet-heading">
        <h2 id={label}>{title}</h2>
        <Button variant="ghost" size="icon" aria-label="Close panel" onClick={() => ref.current?.close()}><X size={18} /></Button>
      </header>
      {children}
    </div>
  </dialog>
}
