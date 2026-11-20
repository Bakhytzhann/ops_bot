import '@fontsource-variable/manrope'
import '@fontsource/ibm-plex-mono/latin-500.css'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App, OpenInTelegram } from './App'
import { createOpsApi } from './api'
import { telegramLaunch } from './telegram'
import './styles.css'

const launch = telegramLaunch()
const api = launch && createOpsApi(launch.headers)
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {launch && api ? <App api={api} host={launch} /> : <OpenInTelegram />}
  </StrictMode>,
)
launch?.ready()
