function iniciarDatePicker(reserved_ranges) {
  const DateTime = easepick.DateTime;

  const bookedDates = reserved_ranges.map((d) => {
    if (Array.isArray(d)) {
      const start = new DateTime(d[0], 'YYYY-MM-DD');
      const end = new DateTime(d[1], 'YYYY-MM-DD');
      return [start, end];
    }
    return new DateTime(d, 'YYYY-MM-DD');
  });

  const picker = new easepick.create({
    element: document.getElementById('datepicker'),
    css: ['https://cdn.jsdelivr.net/npm/@easepick/bundle@1.2.1/dist/index.css', 'https://easepick.com/css/demo_hotelcal.css'],
    lang: 'pt-BR',
    inline: true,
    container: document.getElementById('calendar-container'),
    plugins: ['RangePlugin', 'LockPlugin'],
    RangePlugin: {
      tooltipNumber(num) {
        return num - 1;
      },
      locale: {
        one: 'dia',
        other: 'dias',
      },
    },
    LockPlugin: {
      minDate: new Date(),
      minDays: 1,
      inseparable: true,
      filter(date, picked) {
        if (picked.length === 1) {
          const incl = date.isBefore(picked[0]) ? '[)' : '(]';
          return !picked[0].isSame(date, 'day') && date.inArray(bookedDates, incl);
        }
        return date.inArray(bookedDates, '[)');
      },
    },
  });
}
