/* global SwaggerUIBundle */
SwaggerUIBundle({
  url: '/openapi.json',
  dom_id: '#swagger-ui',
  deepLinking: true,
  validatorUrl: null,
  persistAuthorization: false,
  supportedSubmitMethods: ['get', 'post'],
  requestInterceptor(request) {
    request.headers.Accept = 'application/json'
    return request
  },
})
