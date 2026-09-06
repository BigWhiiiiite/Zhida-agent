export type Experience = { organization: string; role: string; start_date: string; end_date: string; description: string }
export type Project = { name: string; role: string; start_date: string; end_date: string; description: string; technologies: string[] }
export type Education = { school: string; degree: string; major: string; start_date: string; end_date: string }
export type ResumeProfile = {
  name: string; gender: '男' | '女' | '其他' | '未识别'; age: number | null; phone: string; email: string;
  location: string; target_role: string; summary: string; education: Education[]; internships: Experience[];
  projects: Project[]; skills: string[]
}
export type ResumeRecord = { id: string; filename: string; label: string; profile: ResumeProfile; parser: string; created_at: string; updated_at: string }

