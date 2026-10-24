/**
 * 共享组件库统一出口。
 * 业务组件请统一从这里导入，禁止直接 import 单个文件。
 */
export { cn } from './cn';

export { Button } from './Button';
export type { ButtonProps, ButtonVariant, ButtonSize } from './Button';

export { Input } from './Input';
export type { InputProps } from './Input';

export { Textarea } from './Textarea';
export type { TextareaProps } from './Textarea';

export { Select } from './Select';
export type { SelectProps } from './Select';

export { Card } from './Card';
export type { CardProps } from './Card';

export { Badge } from './Badge';
export type { BadgeProps, BadgeVariant } from './Badge';

export { Modal } from './Modal';
export type { ModalProps } from './Modal';

export { ToastProvider, useToast } from './Toast';
export type { ToastItem, ToastType, ToastContextValue } from './Toast';

export { default as ErrorBoundary } from './ErrorBoundary';
export { default as GlobalErrorBanner } from './GlobalErrorBanner';
