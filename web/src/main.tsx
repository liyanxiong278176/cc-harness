import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './cc/app'
import './styles.css'

const root = document.getElementById('root')
if (!root) throw new Error('cc-harness WebUI: missing #root')

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
