import { ref } from 'vue'
import i18n from '../i18n'

// 正则表达式模板
export interface RegexTemplate {
  name: string        // 模板名称（中文）
  nameEn: string      // 英文名称
  pattern: string     // 正则表达式
  description: string // 描述（中文）
  descriptionEn: string // 英文描述
  example: string     // 示例
}

// 常用正则表达式模板列表
const DEFAULT_TEMPLATES: RegexTemplate[] = [
  {
    name: '手机号',
    nameEn: 'Phone',
    pattern: '1[3-9]\\d{9}',
    description: '匹配中国大陆手机号',
    descriptionEn: 'Match Chinese mobile phone number',
    example: '13812345678',
  },
  {
    name: 'IP地址',
    nameEn: 'IP Address',
    pattern: '\\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\\b',
    description: '匹配 IPv4 地址',
    descriptionEn: 'Match IPv4 address',
    example: '192.168.1.1',
  },
  {
    name: '邮箱',
    nameEn: 'Email',
    pattern: '[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}',
    description: '匹配电子邮箱地址',
    descriptionEn: 'Match email address',
    example: 'user@example.com',
  },
  {
    name: 'URL',
    nameEn: 'URL',
    pattern: 'https?://[^\\s"\'<>]+',
    description: '匹配 HTTP/HTTPS URL',
    descriptionEn: 'Match HTTP/HTTPS URL',
    example: 'https://api.example.com/path',
  },
  {
    name: 'JSON对象',
    nameEn: 'JSON Object',
    pattern: '\\{[^{}]*\\}',
    description: '匹配简单 JSON 对象',
    descriptionEn: 'Match simple JSON object',
    example: '{"key": "value"}',
  },
  {
    name: 'JSON数组',
    nameEn: 'JSON Array',
    pattern: '\\[[^\\[\\]]*\\]',
    description: '匹配简单 JSON 数组',
    descriptionEn: 'Match simple JSON array',
    example: '[1, 2, 3]',
  },
  {
    name: '时间戳',
    nameEn: 'Timestamp',
    pattern: '\\d{10,13}',
    description: '匹配 10-13 位时间戳',
    descriptionEn: 'Match 10-13 digit timestamp',
    example: '1699999999',
  },
  {
    name: 'JWT Token',
    nameEn: 'JWT Token',
    pattern: 'eyJ[A-Za-z0-9_-]+\\.eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+',
    description: '匹配 JWT 令牌',
    descriptionEn: 'Match JWT token',
    example: 'eyJhbGciOiJIUzI1NiIs...',
  },
  {
    name: 'UUID',
    nameEn: 'UUID',
    pattern: '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}',
    description: '匹配 UUID 格式',
    descriptionEn: 'Match UUID format',
    example: '550e8400-e29b-41d4-a716-446655440000',
  },
  {
    name: 'Base64',
    nameEn: 'Base64',
    pattern: '[A-Za-z0-9+/]+=*',
    description: '匹配 Base64 编码字符串',
    descriptionEn: 'Match Base64 encoded string',
    example: 'SGVsbG8gV29ybGQ=',
  },
  {
    name: '十六进制',
    nameEn: 'Hex Color',
    pattern: '#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3}',
    description: '匹配十六进制颜色值',
    descriptionEn: 'Match hex color code',
    example: '#FF5733',
  },
  {
    name: 'HTML标签',
    nameEn: 'HTML Tag',
    pattern: '<[^>]+>',
    description: '匹配 HTML 标签',
    descriptionEn: 'Match HTML tag',
    example: '<div class="test">',
  },
]

/**
 * 正则表达式模板 Store
 * - 提供常用正则模板列表
 * - 验证正则表达式有效性
 */
export function useRegexTemplate() {
  // 正则模板列表
  const templates = ref<RegexTemplate[]>(DEFAULT_TEMPLATES)

  /**
   * 验证正则表达式是否有效
   * @param pattern 正则表达式字符串
   * @returns 验证结果：{ valid: boolean, error?: string }
   */
  function validateRegex(pattern: string): { valid: boolean; error?: string } {
    if (!pattern) {
      return { valid: true } // 空正则视为有效（表示不使用正则）
    }
    try {
      new RegExp(pattern)
      return { valid: true }
    } catch (e: any) {
      return { valid: false, error: e.message || i18n.global.t('search.invalidRegexSimple') }
    }
  }

  /**
   * 测试正则表达式是否匹配指定文本
   * @param pattern 正则表达式字符串
   * @param text 要测试的文本
   * @returns 是否匹配
   */
  function testRegex(pattern: string, text: string): boolean {
    if (!pattern || !text) return false
    try {
      const regex = new RegExp(pattern)
      return regex.test(text)
    } catch {
      return false
    }
  }

  /**
   * 查找所有匹配项
   * @param pattern 正则表达式字符串
   * @param text 要搜索的文本
   * @returns 匹配项数组
   */
  function findMatches(pattern: string, text: string): string[] {
    if (!pattern || !text) return []
    try {
      const regex = new RegExp(pattern, 'g')
      const matches: string[] = []
      let match
      while ((match = regex.exec(text)) !== null) {
        matches.push(match[0])
        // 防止无限循环（正则没有前进时）
        if (match[0].length === 0) {
          regex.lastIndex++
        }
      }
      return matches
    } catch {
      return []
    }
  }

  return {
    templates,
    validateRegex,
    testRegex,
    findMatches,
  }
}
