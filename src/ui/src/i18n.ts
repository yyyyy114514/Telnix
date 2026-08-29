import { createI18n } from 'vue-i18n'
import zh from './locales/zh'
import en from './locales/en'

export type Lang = 'zh' | 'en'

const LANG_KEY = 'telnix_lang'

export function getLang(): Lang {
  const saved = localStorage.getItem(LANG_KEY)
  if (saved === 'en' || saved === 'zh') return saved
  // Default: Chinese
  return 'zh'
}

export function setLang(lang: Lang) {
  localStorage.setItem(LANG_KEY, lang)
  i18n.global.locale.value = lang
}

const i18n = createI18n({
  legacy: false,
  locale: getLang(),
  fallbackLocale: 'zh',
  messages: { zh, en },
})

export default i18n
