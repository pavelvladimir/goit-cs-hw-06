import Navigation from '../components/navigation'

const courses = [
  { name: 'Python Core', weeks: 12, technologies: ['Python', 'pip', 'OOP', 'pickle'] },
  {
    name: 'Python Web',
    weeks: 14,
    technologies: ['Docker, Poetry, Pipenv', 'SQLAlchemy, MongoDB', 'Django', 'FastAPI'],
  },
  {
    name: 'Python Data Science',
    weeks: 12,
    technologies: ['NumPy, Pandas', 'Matplotlib, Seaborn', 'TensorFlow', 'Keras'],
  },
] as const

export default function HomePage() {
  return (
    <>
      <Navigation active="home" />
      <main className="container">
        <div className="p-3 pb-md-4 mx-auto text-center">
          <h1 className="display-4 fw-normal">Python</h1>
          <p className="fs-5 text-muted">
            Python легко почати вивчати — і новачкам у програмуванні, і тим, хто вже має досвід з іншими мовами.
          </p>
        </div>
        <div className="row row-cols-1 row-cols-md-3 mb-3 text-center">
          {courses.map((course) => (
            <div className="col" key={course.name}>
              <section
                className={`card mb-4 rounded-3 shadow-sm${course.name === 'Python Web' ? ' border-primary' : ''}`}
                aria-label={course.name}
              >
                <div
                  className={`card-header py-3${course.name === 'Python Web' ? ' text-bg-primary border-primary' : ''}`}
                >
                  <h2 className="h4 my-0 fw-normal">{course.name}</h2>
                </div>
                <div className="card-body">
                  <p className="h1 card-title mb-3">
                    {course.weeks} <small className="text-muted fw-light">тижнів</small>
                  </p>
                  <h3 className="h5">Технології:</h3>
                  <ul className="list-unstyled mt-3 mb-4">
                    {course.technologies.map((technology) => (
                      <li key={technology}>{technology}</li>
                    ))}
                  </ul>
                </div>
              </section>
            </div>
          ))}
        </div>
      </main>
    </>
  )
}
