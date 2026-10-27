export const profileColors = [
  { id: "rose", name: "Пыльная роза", hex: "#C59BA9" },
  { id: "peach", name: "Персиковый", hex: "#C5A18D" },
  { id: "sand", name: "Песочный", hex: "#BDB18C" },
  { id: "sage", name: "Шалфейный", hex: "#9CB39C" },
  { id: "teal", name: "Приглушённая бирюза", hex: "#8EAFAD" },
  { id: "blue", name: "Серо-голубой", hex: "#99ACC5" },
  { id: "lavender", name: "Лавандовый", hex: "#B0A0C5" },
];

export function profileColor(color: string | undefined, index = 0) {
  return profileColors.find((item) => item.id === color) ?? profileColors[index % profileColors.length];
}
